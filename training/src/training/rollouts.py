"""Colocated vLLM async engine and multi-trajectory agent loop.

One ``RolloutWorker`` Ray actor owns:
  - a single ``AsyncLLM`` engine (continuous batching across concurrent ``generate()`` calls)
  - per-trajectory agent loops (generate → parse tools → execute → append observations)

Concurrency:
  - Across trajectories: ``asyncio.gather`` with a semaphore cap
  - Within a trajectory: turns are sequential; tool calls in one turn run in parallel
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import ray

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
    env_id: str
    env_actor: ray.actor.ActorHandle
    messages: list[dict[str, Any]]
    rollout_index: int
    expected_group_size: int
    policy_version: int
    sampling_params: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] | None = None


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
    num_turns: int
    reward: float | None = None
    done_reason: str = "completed"


@dataclass
class Session:
    """Per-trajectory state for the agent loop."""

    request_id: str
    group_id: str
    task_id: str
    env_id: str
    env_actor: ray.actor.ActorHandle
    rollout_index: int
    expected_group_size: int
    policy_version: int
    prompt_ids: list[int]
    messages: list[dict[str, Any]]
    response_ids: list[int] = field(default_factory=list)
    response_mask: list[int] = field(default_factory=list)
    assistant_turns: int = 0
    done: bool = False


# TODO: Look into using renderers for TITO processing
@ray.remote(num_gpus=1, resources={"rollouts": 1})
class RolloutWorker:
    """GPU worker: AsyncLLM inference + asyncio agent-loop orchestration."""

    def __init__(
        self,
        model_path: str | None = None,
        *,
        max_model_len: int = 8192,
        max_num_seqs: int = 64,
        max_concurrent_trajectories: int = 32,
        max_tokens_per_turn: int = 512,
        max_assistant_turns: int = 8,
    ) -> None:
        from transformers import AutoTokenizer
        from vllm import SamplingParams
        from vllm.engine.arg_utils import AsyncEngineArgs
        from vllm.inputs import TokensPrompt

        try:
            from vllm.v1.engine.async_llm import AsyncLLM
        except ImportError:
            from vllm import AsyncLLMEngine as AsyncLLM

        resolved_model = model_path or os.environ.get(
            "MODEL_PATH", "/models/Qwen2.5-7B"
        )

        self._sampling_params_cls = SamplingParams
        self._tokens_prompt_cls = TokensPrompt
        self.max_model_len = max_model_len
        self.max_tokens_per_turn = max_tokens_per_turn
        self.max_assistant_turns = max_assistant_turns

        engine_args = AsyncEngineArgs(
            model=resolved_model,
            max_model_len=max_model_len,
            enable_prefix_caching=True,
            max_num_seqs=max_num_seqs,
        )
        self.engine = AsyncLLM.from_engine_args(engine_args)
        self._start_metrics_server()

        self.tokenizer = AutoTokenizer.from_pretrained(
            resolved_model,
            local_files_only=True,
            trust_remote_code=True,
        )
        if self.tokenizer.pad_token is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        self._sem = asyncio.Semaphore(max_concurrent_trajectories)
        self.policy_version = 0

    def _start_metrics_server(self) -> None:
        """Expose vLLM's in-process Prometheus metrics for the collector scrape job.

        vLLM registers its stats in the default prometheus_client registry of
        this actor process; nothing serves them until we start an exposition
        server. The port must match the collector's vllm scrape job.
        """
        import logging

        from prometheus_client import start_http_server

        port = int(os.environ.get("GRL_VLLM_METRICS_PORT", "9090"))
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
        """Run one trajectory against the environment bound to this request."""
        sampling_params = dict(request.sampling_params or {"temperature": 1.0, "top_p": 1.0})

        async with self._sem:
            prompt_ids = self._tokenize_chat(request.messages, tools=request.tools)
            session = Session(
                request_id=uuid.uuid4().hex,
                group_id=request.group_id,
                task_id=request.task_id,
                env_id=request.env_id,
                env_actor=request.env_actor,
                rollout_index=request.rollout_index,
                expected_group_size=request.expected_group_size,
                policy_version=request.policy_version,
                prompt_ids=prompt_ids,
                messages=list(request.messages),
            )
            session = await self._run_trajectory(session, sampling_params)

        prompt_len = len(session.prompt_ids) - len(session.response_ids)
        return RolloutResult(
            group_id=session.group_id,
            task_id=session.task_id,
            env_id=session.env_id,
            rollout_index=session.rollout_index,
            expected_group_size=session.expected_group_size,
            policy_version=session.policy_version,
            request_id=session.request_id,
            prompt_ids=session.prompt_ids[:prompt_len],
            response_ids=session.response_ids,
            response_mask=session.response_mask,
            num_turns=session.assistant_turns,
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
            "Use run_rollout() so each concurrent session has its own environment actor."
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

            turn_ids = await self._generate_once(
                request_id=session.request_id,
                prompt_ids=session.prompt_ids,
                sampling_params=sampling_params,
            )
            session.assistant_turns += 1
            session.prompt_ids += turn_ids
            session.response_ids += turn_ids
            session.response_mask += [1] * len(turn_ids)

            tool_calls = self._parse_tool_calls(turn_ids)
            if not tool_calls:
                session.done = True
                break

            tool_messages = await asyncio.gather(
                *[self._execute_tool(session, tc) for tc in tool_calls]
            )
            session.messages.extend(tool_messages)

            tool_token_ids = self._tokenize_tool_messages(tool_messages)
            session.prompt_ids += tool_token_ids
            session.response_ids += tool_token_ids
            session.response_mask += [0] * len(tool_token_ids)

        return session

    async def _generate_once(
        self,
        *,
        request_id: str,
        prompt_ids: list[int],
        sampling_params: dict[str, Any],
    ) -> list[int]:
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

        sp = self._sampling_params_cls(max_tokens=max_tokens, **params)
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
            return []

        return list(final.outputs[0].token_ids)

    async def _execute_tool(
        self,
        session: Session,
        tool_call: ToolCall,
    ) -> dict[str, Any]:
        """Dispatch a tool call to the VM owned by this session."""
        result = await session.env_actor.execute.remote(
            session.env_id,
            tool_call.name,
            tool_call.arguments,
        )
        if isinstance(result, dict) and "role" in result:
            return result
        return {"role": "tool", "content": str(result)}

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
