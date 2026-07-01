"""Colocated vLLM async engine and multi-trajectory agent loop.

One ``RolloutWorker`` Ray actor owns:
  - a single ``AsyncLLM`` engine (continuous batching across concurrent ``generate()`` calls)
  - per-trajectory agent loops (generate → parse tools → execute → append observations)

Concurrency:
  - Across trajectories: ``asyncio.gather`` with a semaphore cap
  - Within a trajectory: turns are sequential; one tool call per turn
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, cast

import ray
from opentelemetry.metrics import Observation
from renderers import Message, ParsedResponse, ToolSpec
from renderers import Renderer as BaseRenderer
from renderers.base import MODEL_RENDERER_MAP, create_renderer

from grl_config.training import GRLConfig
from training.environments import (
    EnvironmentSession,
    InfraError,
    RetryConfig,
    RpcTimeouts,
    SUBMIT_TOOL,
)
from training.telemetry import (
    counter,
    histogram,
    init_telemetry,
    log_trajectory,
    observable_gauge,
    record_duration,
    span,
)


def _create_base_renderer(tokenizer: Any, model_id: str) -> BaseRenderer:
    """Pick the renderer for ``model_id`` (hand-coded map, else default).

    We resolve by ``model_id`` rather than letting ``create_renderer`` auto
    -detect, because the tokenizer is loaded from a local cache path whose
    ``name_or_path`` won't match the canonical HF id in ``MODEL_RENDERER_MAP``.
    """
    name = Renderer.name_for_model(model_id)
    if name == "default":
        return create_renderer(tokenizer, renderer="default", tool_parser="qwen3")
    return create_renderer(tokenizer, renderer=name)


class Renderer:
    """Integrates the ``renderers`` package into GRL rollouts.

    The ``renderers`` package is intentionally low-level: it renders messages to
    tokens, parses completions, and bridges turns while preserving sampled tokens.
    It does *not* convert OpenAI-shaped tools/tool-calls to its own types, so this
    wrapper covers exactly that seam and nothing more.
    """

    def __init__(self, tokenizer: Any, model_id: str) -> None:
        self._base = _create_base_renderer(tokenizer, model_id)

    @staticmethod
    def name_for_model(model_id: str) -> str:
        """Resolve a hand-coded renderer name from the HF repo id."""
        return MODEL_RENDERER_MAP.get(model_id, "default")

    @property
    def stop_token_ids(self) -> list[int]:
        return self._base.get_stop_token_ids()

    @property
    def base(self) -> BaseRenderer:
        return self._base

    def to_tools(self, tools: list[dict[str, Any]] | None) -> list[ToolSpec] | None:
        """Unwrap OpenAI ``{"function": {...}}`` tool schemas into ``ToolSpec``s."""
        if not tools:
            return None
        specs: list[ToolSpec] = []
        for tool in tools:
            fn = tool.get("function")
            if isinstance(fn, dict):
                specs.append(
                    ToolSpec(
                        name=str(fn["name"]),
                        description=str(fn.get("description") or ""),
                        parameters=fn.get("parameters") or {},
                    )
                )
            else:
                specs.append(cast(ToolSpec, tool))
        return specs

    @staticmethod
    def _to_messages(messages: list[dict[str, Any]]) -> list[Message]:
        """Drop ``None`` values so optional keys stay absent for the renderer."""
        return [
            cast(Message, {k: v for k, v in m.items() if v is not None}) for m in messages
        ]

    @staticmethod
    def _name_and_arguments(tool_call: dict[str, Any]) -> tuple[str, str]:
        """Extract ``(name, arguments_json)`` from a parsed tool-call dict."""
        fn = tool_call.get("function", tool_call)
        name = str(fn.get("name") or "")
        arguments = fn.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {})
        return name, arguments

    def parsed_tool_calls(self, parsed: ParsedResponse) -> list[tuple[str, str]]:
        """Return at most one ``(name, arguments_json)`` pair from a completion."""
        for tool_call in parsed.tool_calls or []:
            name, arguments = self._name_and_arguments(tool_call)
            if name:
                return [(name, arguments)]
        return []

    def render_prompt_ids(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        add_generation_prompt: bool = True,
    ) -> list[int]:
        return self._base.render_ids(
            self._to_messages(messages),
            tools=self.to_tools(tools),
            add_generation_prompt=add_generation_prompt,
        )

    def parse_tool_calls(self, token_ids: list[int]) -> list[tuple[str, str]]:
        parsed = self._base.parse_response(token_ids)
        return self.parsed_tool_calls(parsed)

    def bridge_after_tool(
        self,
        turn_prompt_ids: list[int],
        generation_token_ids: list[int],
        tool_message: dict[str, Any],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> Any:
        return self._base.bridge_to_next_turn(
            turn_prompt_ids,
            generation_token_ids,
            self._to_messages([tool_message]),
            tools=self.to_tools(tools),
        )


@dataclass
class ToolCall:
    name: str
    arguments: str


@dataclass
class RolloutRequest:
    group_id: str
    task_id: str
    rollout_index: int
    expected_group_size: int
    policy_version: int
    sampling_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RolloutResult:
    group_id: str
    task_id: str
    env_id: str
    rollout_index: int
    expected_group_size: int
    # Latest policy version observed while generating this rollout. For async
    # weight updates this can differ from policy_version_start.
    policy_version_current: int
    request_id: str
    prompt_ids: list[int]
    response_ids: list[int]
    response_mask: list[int]
    # Per-token logprobs from vLLM at rollout time; aligned 1:1 with response_ids.
    inference_logprobs: list[float]
    num_turns: int
    reward: float | None = None
    done_reason: str = "completed"
    policy_version_start: int | None = None


@dataclass(frozen=True)
class PolicyWeightsRef:
    """Nested wrapper so Ray sends the ObjectRef itself, not its value."""

    ref: ray.ObjectRef


@dataclass
class GenerationResult:
    token_ids: list[int]
    logprobs: list[float]


@dataclass
class Session:
    """Per-trajectory state for the agent loop."""

    request_id: str
    group_id: str
    task_id: str
    env: EnvironmentSession
    rollout_index: int
    expected_group_size: int
    policy_version_start: int
    policy_version_current: int
    prompt_ids: list[int]
    response_ids: list[int] = field(default_factory=list)
    response_mask: list[int] = field(default_factory=list)
    inference_logprobs: list[float] = field(default_factory=list)
    assistant_turns: int = 0
    submitted: bool = False
    done: bool = False


@ray.remote(num_gpus=1, resources={"rollouts": 1})
class RolloutWorker:
    """GPU worker: AsyncLLM inference + asyncio agent-loop orchestration."""

    def __init__(self, config: dict[str, Any], *, run_id: str = "") -> None:
        cfg = GRLConfig.model_validate(config)
        init_telemetry(
            "rollout",
            run_id,
            otel_endpoint=cfg.telemetry.otel_endpoint,
        )

        from transformers import AutoTokenizer
        from vllm import SamplingParams
        from vllm.engine.arg_utils import AsyncEngineArgs
        from vllm.inputs import TokensPrompt

        try:
            from vllm.v1.engine.async_llm import AsyncLLM
        except ImportError:
            from vllm import AsyncLLMEngine as AsyncLLM

        rollout = cfg.rollout
        model_id = cfg.model
        model_path = cfg.resolved_model_path()

        self._sampling_params_cls = SamplingParams
        self._tokens_prompt_cls = TokensPrompt
        self.max_model_len = rollout.max_model_len
        self.max_tokens_per_turn = rollout.max_tokens_per_turn
        self.max_assistant_turns = rollout.max_assistant_turns
        self.generation_timeout_secs = rollout.generation_timeout_secs
        self.trajectory_timeout_secs = rollout.trajectory_timeout_secs
        self.env_server_addr = cfg.environment.server_addr
        env_retry = cfg.environment.retry
        self.env_retry = RetryConfig(
            max_attempts=env_retry.max_attempts,
            initial_backoff_secs=env_retry.initial_backoff_secs,
            max_backoff_secs=env_retry.max_backoff_secs,
        )
        self.env_rpc_timeouts = RpcTimeouts.from_config(cfg.environment.rpc_timeouts)

        engine_args = AsyncEngineArgs(
            model=str(model_path),
            max_model_len=rollout.max_model_len,
            enable_prefix_caching=rollout.enable_prefix_caching,
            max_num_seqs=rollout.max_num_seqs,
        )
        self.engine = AsyncLLM.from_engine_args(engine_args)
        self._start_metrics_server(rollout.vllm_metrics_port)

        self.tokenizer = AutoTokenizer.from_pretrained(
            str(model_path),
            local_files_only=True,
            trust_remote_code=True,
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self.renderer = Renderer(self.tokenizer, model_id)

        self._sem = asyncio.Semaphore(rollout.max_concurrent_trajectories)
        self.policy_version = 0

        # Trajectories currently holding a semaphore slot. Tracked explicitly
        # (rather than peeking at the semaphore internals) so the observable
        # gauge callback stays a cheap synchronous read.
        self._in_flight = 0
        observable_gauge(
            "grl.rollout.in_flight",
            lambda _options: [Observation(self._in_flight)],
            description="Concurrent rollout trajectories on this worker",
        )

    def _start_metrics_server(self, port: int) -> None:
        """Expose vLLM's in-process Prometheus metrics for the collector scrape job.

        vLLM registers its stats in the default prometheus_client registry of
        this actor process; nothing serves them until we start an exposition
        server. The port must match the collector's vllm scrape job.
        """
        import logging

        from prometheus_client import start_http_server

        try:
            start_http_server(port)
        except OSError as exc:
            # Another RolloutWorker in this pod already bound the port; its
            # registry doesn't include this process's metrics, so they would
            # be missing from scrapes.
            logging.getLogger(__name__).warning(
                "vLLM metrics server not started on port %s: %s", port, exc
            )

    def get_policy_version(self) -> int:
        return self.policy_version

    async def apply_policy_update(
        self,
        policy_version: int,
        weights_ref: PolicyWeightsRef | None = None,
    ) -> None:
        if policy_version <= self.policy_version:
            return
        if weights_ref is None:
            self.policy_version = policy_version
            return

        state_dict = await asyncio.to_thread(ray.get, weights_ref.ref)
        await self._reload_vllm_weights(policy_version, state_dict)

    async def _reload_vllm_weights(
        self,
        policy_version: int,
        state_dict: dict[str, Any],
    ) -> None:
        """Load a CPU state dict into the live vLLM engine in place."""

        with record_duration("grl.rollout.weight_reload.duration"):
            await self.engine.pause_generation(mode="keep", clear_cache=True)
            try:
                await self.engine.collective_rpc(
                    "reload_weights",
                    kwargs={"weights_iterator": list(state_dict.items())},
                )
                self.policy_version = policy_version
            finally:
                await self.engine.resume_generation()

    async def run_rollout(self, request: RolloutRequest) -> RolloutResult:
        """Run one trajectory: Create → Execute* → Evaluate → Teardown."""
        start = time.perf_counter()
        with span(
            "rollout",
            task_id=request.task_id,
            group_id=request.group_id,
            rollout_index=request.rollout_index,
            policy_version_start=request.policy_version,
        ) as current:
            try:
                result = await asyncio.wait_for(
                    self._run_rollout_inner(request),
                    timeout=self.trajectory_timeout_secs,
                )
            except asyncio.TimeoutError:
                counter("grl.rollout.truncated").add(1, {"cause": "trajectory_timeout"})
                result = RolloutResult(
                    group_id=request.group_id,
                    task_id=request.task_id,
                    env_id="",
                    rollout_index=request.rollout_index,
                    expected_group_size=request.expected_group_size,
                    policy_version_current=request.policy_version,
                    request_id="",
                    prompt_ids=[],
                    response_ids=[],
                    response_mask=[],
                    inference_logprobs=[],
                    num_turns=0,
                    reward=None,
                    done_reason="infra_error",
                    policy_version_start=request.policy_version,
                )

            current.set_attribute("done_reason", result.done_reason)
            current.set_attribute("num_turns", result.num_turns)
            if result.reward is not None:
                current.set_attribute("reward", float(result.reward))
            self._record_rollout_metrics(result, time.perf_counter() - start)
            self._log_trajectory(result)
            return result

    def _record_rollout_metrics(self, result: RolloutResult, duration: float) -> None:
        counter("grl.rollout.completed").add(1, {"done_reason": result.done_reason})
        histogram("grl.rollout.duration", unit="s").record(duration)
        histogram("grl.rollout.num_turns").record(result.num_turns)
        histogram("grl.rollout.response_tokens").record(len(result.response_ids))
        histogram("grl.rollout.prompt_tokens").record(len(result.prompt_ids))
        if result.reward is not None:
            histogram("grl.rollout.reward").record(float(result.reward))
        if result.policy_version_start is not None:
            staleness = result.policy_version_current - result.policy_version_start
            histogram("grl.rollout.policy_staleness").record(max(0, staleness))

    def _log_trajectory(self, result: RolloutResult) -> None:
        log_trajectory(
            task_id=result.task_id,
            group_id=result.group_id,
            rollout_index=result.rollout_index,
            policy_version_start=result.policy_version_start or 0,
            policy_version_current=result.policy_version_current,
            num_turns=result.num_turns,
            reward=result.reward,
            done_reason=result.done_reason,
            prompt_tokens=len(result.prompt_ids),
            response_tokens=len(result.response_ids),
            prompt=self._safe_decode(result.prompt_ids),
            response=self._safe_decode(result.response_ids),
        )

    def _safe_decode(self, token_ids: list[int]) -> str:
        if not token_ids:
            return ""
        try:
            return self.tokenizer.decode(token_ids)
        except Exception:
            return ""

    async def _run_rollout_inner(self, request: RolloutRequest) -> RolloutResult:
        sampling_params = dict(request.sampling_params or {"temperature": 1.0, "top_p": 1.0})
        reward: float | None = None
        done_reason = "completed"
        session: Session | None = None

        async with self._sem:
            self._in_flight += 1
            try:
                env = await EnvironmentSession.create(
                    request.task_id,
                    addr=self.env_server_addr,
                    retry=self.env_retry,
                    rpc_timeouts=self.env_rpc_timeouts,
                )
                try:
                    prompt_ids = self.renderer.render_prompt_ids(
                        env.initial_messages,
                        tools=env.tools,
                    )
                    session = Session(
                        request_id=uuid.uuid4().hex,
                        group_id=request.group_id,
                        task_id=request.task_id,
                        env=env,
                        rollout_index=request.rollout_index,
                        expected_group_size=request.expected_group_size,
                        policy_version_start=request.policy_version,
                        policy_version_current=request.policy_version,
                        prompt_ids=list(prompt_ids),
                    )
                    try:
                        session = await self._run_trajectory(
                            session, sampling_params, env.tools
                        )
                    except InfraError:
                        done_reason = "infra_error"
                    try:
                        result = await env.evaluate()
                        if result.infra_error:
                            done_reason = "infra_error"
                        reward = result.reward
                    except InfraError:
                        done_reason = "infra_error"
                finally:
                    await asyncio.shield(env.teardown())
            finally:
                self._in_flight -= 1

        assert session is not None
        prompt_len = len(session.prompt_ids) - len(session.response_ids)
        return RolloutResult(
            group_id=session.group_id,
            task_id=session.task_id,
            env_id=session.env.env_id,
            rollout_index=session.rollout_index,
            expected_group_size=session.expected_group_size,
            policy_version_current=session.policy_version_current,
            request_id=session.request_id,
            prompt_ids=session.prompt_ids[:prompt_len],
            response_ids=session.response_ids,
            response_mask=session.response_mask,
            inference_logprobs=session.inference_logprobs,
            num_turns=session.assistant_turns,
            reward=reward,
            done_reason=done_reason,
            policy_version_start=session.policy_version_start,
        )

    async def generate_batch(
        self,
        prompts: list[list[dict[str, Any]]],
        sampling_params: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run agent loops for a batch of chat prompts concurrently.

        Args:
            prompts: Batch of OpenAI-style message lists (``raw_prompt`` per sample).
            sampling_params: vLLM sampling kwargs (temperature, top_p, etc.).
            tools: Optional tool schemas passed to the chat template.

        Returns:
            One dict per sample with ``prompt_ids``, ``response_ids``, ``response_mask``,
            and ``request_id``. ``response_mask`` is 1 for model tokens, 0 for tool tokens.
        """
        raise NotImplementedError(
            "Use run_rollout() so each concurrent session has its own environment."
        )

    async def _run_trajectory(
        self,
        session: Session,
        sampling_params: dict[str, Any],
        tools: list[dict[str, Any]] | None,
    ) -> Session:
        while not session.done:
            if session.assistant_turns >= self.max_assistant_turns:
                counter("grl.rollout.truncated").add(1, {"cause": "max_turns"})
                session.done = True
                break

            turn_prompt_ids = list(session.prompt_ids)
            if len(turn_prompt_ids) >= self.max_model_len:
                counter("grl.rollout.truncated").add(1, {"cause": "model_len"})
                session.done = True
                break

            generation_policy_start = self.policy_version
            if session.assistant_turns == 0:
                session.policy_version_start = generation_policy_start
            try:
                with span("generate"), record_duration(
                    "grl.rollout.generation.duration"
                ):
                    generation = await asyncio.wait_for(
                        self._generate_once(
                            request_id=session.request_id,
                            prompt_ids=turn_prompt_ids,
                            sampling_params=sampling_params,
                        ),
                        timeout=self.generation_timeout_secs,
                    )
            except asyncio.TimeoutError:
                counter("grl.rollout.truncated").add(1, {"cause": "gen_timeout"})
                session.done = True
                break

            generation_policy_end = self.policy_version
            session.policy_version_current = max(
                session.policy_version_current,
                generation_policy_start,
                generation_policy_end,
            )
            session.assistant_turns += 1
            tool_call_specs = self.renderer.parse_tool_calls(generation.token_ids)

            session.response_ids += generation.token_ids
            session.response_mask += [1] * len(generation.token_ids)
            session.inference_logprobs += generation.logprobs

            if not tool_call_specs:
                session.prompt_ids = turn_prompt_ids + generation.token_ids
                session.done = True
                break

            tool_call = ToolCall(name=tool_call_specs[0][0], arguments=tool_call_specs[0][1])
            tool_message = await self._execute_tool(session, tool_call)

            # Advance the prompt via the renderer's bridge: it keeps the
            # sampled completion tokens verbatim (required for token-exact RL
            # credit assignment) and appends only the scaffolding + tool
            # message the next turn adds. The returned sequence is guaranteed
            # to start with ``turn_prompt_ids + generation.token_ids``, so the
            # slice past ``anchor`` is exactly the tool-observation suffix.
            bridged = self.renderer.bridge_after_tool(
                turn_prompt_ids,
                generation.token_ids,
                tool_message,
                tools=tools,
            )
            if bridged is None:
                raise RuntimeError(
                    f"{type(self.renderer.base).__name__} cannot bridge a tool "
                    "observation without re-rendering sampled tokens; use a "
                    "model-specific renderer for multi-turn RL rollouts."
                )
            next_prompt_ids = list(bridged.token_ids)
            anchor = len(turn_prompt_ids) + len(generation.token_ids)
            tool_suffix = next_prompt_ids[anchor:]
            session.prompt_ids = next_prompt_ids
            session.response_ids += tool_suffix
            session.response_mask += [0] * len(tool_suffix)
            session.inference_logprobs += [0.0] * len(tool_suffix)

            if session.submitted:
                session.done = True

        return session

    async def _generate_once(
        self,
        *,
        request_id: str,
        prompt_ids: list[int],
        sampling_params: dict[str, Any],
    ) -> GenerationResult:
        max_possible = self.max_model_len - len(prompt_ids)
        if max_possible < 1:
            raise ValueError(
                f"Prompt length {len(prompt_ids)} leaves no room within "
                f"max_model_len={self.max_model_len}"
            )

        params = dict(sampling_params)
        max_tokens = min(self.max_tokens_per_turn, max_possible)
        if "max_tokens" in params:
            max_tokens = min(int(params.pop("max_tokens")), max_possible)
        elif "max_new_tokens" in params:
            max_tokens = min(int(params.pop("max_new_tokens")), max_possible)
        max_tokens = max(1, max_tokens)

        sampling_kwargs: dict[str, Any] = {
            "max_tokens": max_tokens,
            "logprobs": 1,
            **params,
        }
        if self.renderer.stop_token_ids:
            sampling_kwargs["stop_token_ids"] = self.renderer.stop_token_ids

        sp = self._sampling_params_cls(**sampling_kwargs)
        prompt = self._tokens_prompt_cls(prompt_token_ids=prompt_ids)

        generator = self.engine.generate(
            prompt=prompt,
            sampling_params=sp,
            request_id=request_id,
        )
        final = None
        async for output in generator:
            final = output
        if final is None or not final.outputs:
            return GenerationResult(token_ids=[], logprobs=[])

        completion = final.outputs[0]
        token_ids = list(completion.token_ids)
        logprobs = self._extract_sample_logprobs(token_ids, completion.logprobs)
        return GenerationResult(token_ids=token_ids, logprobs=logprobs)

    def _extract_sample_logprobs(
        self,
        token_ids: list[int],
        logprobs_per_step: list[dict[int, Any]] | None,
    ) -> list[float]:
        if logprobs_per_step is None:
            return [0.0] * len(token_ids)

        result: list[float] = []
        for i, token_id in enumerate(token_ids):
            if i >= len(logprobs_per_step) or logprobs_per_step[i] is None:
                result.append(0.0)
                continue
            entry = logprobs_per_step[i].get(token_id)
            if entry is None:
                result.append(0.0)
            elif hasattr(entry, "logprob"):
                result.append(float(entry.logprob))
            else:
                result.append(float(entry))
        return result

    async def _execute_tool(
        self,
        session: Session,
        tool_call: ToolCall,
    ) -> dict[str, Any]:
        """Dispatch one tool call to the environment owned by this session."""
        counter("grl.rollout.tool_calls").add(1, {"tool": tool_call.name})
        if tool_call.name == SUBMIT_TOOL:
            session.submitted = True
        return await session.env.execute(tool_call.name, tool_call.arguments)
