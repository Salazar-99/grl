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
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import ray

from training.config import GRLConfig
from training.environments import (
    EnvironmentSession,
    InfraError,
    RetryConfig,
    RpcTimeouts,
    SUBMIT_TOOL,
)
from training.telemetry import init_telemetry

# Hermes-style tool call blocks (Qwen2.5 tool format); extend when adding parsers.
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
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
    policy_version: int
    request_id: str
    prompt_ids: list[int]
    response_ids: list[int]
    response_mask: list[int]
    # Per-token logprobs from vLLM at rollout time; aligned 1:1 with response_ids.
    inference_logprobs: list[float]
    num_turns: int
    reward: float | None = None
    done_reason: str = "completed"


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
    policy_version: int
    prompt_ids: list[int]
    messages: list[dict[str, Any]]
    response_ids: list[int] = field(default_factory=list)
    response_mask: list[int] = field(default_factory=list)
    inference_logprobs: list[float] = field(default_factory=list)
    assistant_turns: int = 0
    submitted: bool = False
    done: bool = False


# TODO: Look into using renderers for TITO processing
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
        resolved_model = cfg.model.path

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
            model=resolved_model,
            max_model_len=rollout.max_model_len,
            enable_prefix_caching=rollout.enable_prefix_caching,
            max_num_seqs=rollout.max_num_seqs,
        )
        self.engine = AsyncLLM.from_engine_args(engine_args)
        self._start_metrics_server(rollout.vllm_metrics_port)

        self.tokenizer = AutoTokenizer.from_pretrained(
            resolved_model,
            local_files_only=True,
            trust_remote_code=True,
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self._sem = asyncio.Semaphore(rollout.max_concurrent_trajectories)
        self.policy_version = 0

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

    def apply_policy_update(
        self,
        policy_version: int,
        weights_ref: ray.ObjectRef | None = None,
    ) -> None:
        self.policy_version = max(self.policy_version, policy_version)
        # TODO: hot-load weights into vLLM when that path is available.

    async def run_rollout(self, request: RolloutRequest) -> RolloutResult:
        """Run one trajectory: Create → Execute* → Evaluate → Teardown."""
        try:
            return await asyncio.wait_for(
                self._run_rollout_inner(request),
                timeout=self.trajectory_timeout_secs,
            )
        except asyncio.TimeoutError:
            return RolloutResult(
                group_id=request.group_id,
                task_id=request.task_id,
                env_id="",
                rollout_index=request.rollout_index,
                expected_group_size=request.expected_group_size,
                policy_version=request.policy_version,
                request_id="",
                prompt_ids=[],
                response_ids=[],
                response_mask=[],
                inference_logprobs=[],
                num_turns=0,
                reward=None,
                done_reason="infra_error",
            )

    async def _run_rollout_inner(self, request: RolloutRequest) -> RolloutResult:
        sampling_params = dict(request.sampling_params or {"temperature": 1.0, "top_p": 1.0})
        reward: float | None = None
        done_reason = "completed"
        session: Session | None = None

        async with self._sem:
            env = await EnvironmentSession.create(
                request.task_id,
                addr=self.env_server_addr,
                retry=self.env_retry,
                rpc_timeouts=self.env_rpc_timeouts,
            )
            try:
                messages = env.initial_messages
                tools = env.tools
                prompt_ids = self._tokenize_chat(messages, tools=tools)
                session = Session(
                    request_id=uuid.uuid4().hex,
                    group_id=request.group_id,
                    task_id=request.task_id,
                    env=env,
                    rollout_index=request.rollout_index,
                    expected_group_size=request.expected_group_size,
                    policy_version=request.policy_version,
                    prompt_ids=prompt_ids,
                    messages=list(messages),
                )
                try:
                    session = await self._run_trajectory(session, sampling_params)
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

        assert session is not None
        prompt_len = len(session.prompt_ids) - len(session.response_ids)
        return RolloutResult(
            group_id=session.group_id,
            task_id=session.task_id,
            env_id=session.env.env_id,
            rollout_index=session.rollout_index,
            expected_group_size=session.expected_group_size,
            policy_version=session.policy_version,
            request_id=session.request_id,
            prompt_ids=session.prompt_ids[:prompt_len],
            response_ids=session.response_ids,
            response_mask=session.response_mask,
            inference_logprobs=session.inference_logprobs,
            num_turns=session.assistant_turns,
            reward=reward,
            done_reason=done_reason,
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
    ) -> Session:
        while not session.done:
            if session.assistant_turns >= self.max_assistant_turns:
                session.done = True
                break

            try:
                generation = await asyncio.wait_for(
                    self._generate_once(
                        request_id=session.request_id,
                        prompt_ids=session.prompt_ids,
                        sampling_params=sampling_params,
                    ),
                    timeout=self.generation_timeout_secs,
                )
            except asyncio.TimeoutError:
                session.done = True
                break
            session.assistant_turns += 1
            session.prompt_ids += generation.token_ids
            session.response_ids += generation.token_ids
            session.response_mask += [1] * len(generation.token_ids)
            session.inference_logprobs += generation.logprobs

            tool_calls = self._parse_tool_calls(generation.token_ids)
            if not tool_calls:
                session.done = True
                break

            tool_message = await self._execute_tool(session, tool_calls[0])
            session.messages.append(tool_message)
            tool_token_ids = self._tokenize_tool_messages([tool_message])
            session.prompt_ids += tool_token_ids
            session.response_ids += tool_token_ids
            session.response_mask += [0] * len(tool_token_ids)
            session.inference_logprobs += [0.0] * len(tool_token_ids)

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

        sp = self._sampling_params_cls(max_tokens=max_tokens, logprobs=1, **params)
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
        if tool_call.name == SUBMIT_TOOL:
            session.submitted = True
        return await session.env.execute(tool_call.name, tool_call.arguments)

    def _parse_tool_calls(self, token_ids: list[int]) -> list[ToolCall]:
        text = self.tokenizer.decode(token_ids, skip_special_tokens=False)
        calls: list[ToolCall] = []
        for match in _TOOL_CALL_RE.finditer(text):
            try:
                payload = json.loads(match.group(1))
                name = payload.get("name")
                arguments = payload.get("arguments", {})
                if isinstance(arguments, dict):
                    arguments = json.dumps(arguments)
                if name:
                    calls.append(ToolCall(name=name, arguments=str(arguments)))
                    break
            except (json.JSONDecodeError, TypeError):
                continue
        return calls

    def _normalize_token_ids(self, tokenized: Any) -> list[int]:
        if hasattr(tokenized, "tolist"):
            return tokenized.tolist()
        return list(tokenized)

    def _tokenize_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> list[int]:
        tokenized = self.tokenizer.apply_chat_template(
            messages,
            tools=tools,
            add_generation_prompt=True,
            tokenize=True,
        )
        return self._normalize_token_ids(tokenized)

    def _tokenize_tool_messages(self, tool_messages: list[dict[str, Any]]) -> list[int]:
        tokenized = self.tokenizer.apply_chat_template(
            tool_messages,
            add_generation_prompt=False,
            tokenize=True,
        )
        return self._normalize_token_ids(tokenized)
