import {
  ParsedTrajectory,
  ParserWarning,
  ToolCall,
  ToolDef,
  ToolResult,
  TrajectoryBody,
  TranscriptExchange,
} from './types';

const IM_START = '<|im_start|>';
const IM_END = '<|im_end|>';
const THINK_OPEN = '<think>';
const THINK_CLOSE = '</think>';
const TOOL_CALL_OPEN = '<tool_call>';
const TOOL_CALL_CLOSE = '</tool_call>';

interface RawTurn {
  role: 'system' | 'user' | 'assistant' | 'unknown';
  payload: string;
  complete: boolean;
}

function pushWarning(warnings: ParserWarning[], warning: ParserWarning): void {
  if (!warnings.includes(warning)) {
    warnings.push(warning);
  }
}

export function parseBody(raw: string): { body: TrajectoryBody; warnings: ParserWarning[] } {
  const warnings: ParserWarning[] = [];
  try {
    const parsed = JSON.parse(raw) as Partial<TrajectoryBody>;
    return {
      body: {
        prompt: typeof parsed.prompt === 'string' ? parsed.prompt : '',
        response: typeof parsed.response === 'string' ? parsed.response : '',
      },
      warnings,
    };
  } catch {
    pushWarning(warnings, 'malformed_body_json');
    return { body: { prompt: '', response: '' }, warnings };
  }
}

function splitTurns(text: string, warnings: ParserWarning[]): { prefix: string; turns: RawTurn[] } {
  if (!text) {
    return { prefix: '', turns: [] };
  }

  if (!text.includes(IM_START)) {
    return {
      prefix: '',
      turns: [
        {
          role: 'assistant',
          payload: text,
          complete: text.includes(IM_END),
        },
      ],
    };
  }

  const firstStart = text.indexOf(IM_START);
  const prefix = text.slice(0, firstStart);
  const parts = text.slice(firstStart).split(IM_START).slice(1);
  const turns = parts.map((part, index) => {
    const nl = part.indexOf('\n');
    const roleRaw = (nl >= 0 ? part.slice(0, nl) : part).trim();
    const rest = nl >= 0 ? part.slice(nl + 1) : '';
    const role: RawTurn['role'] =
      roleRaw === 'system' || roleRaw === 'user' || roleRaw === 'assistant' ? roleRaw : 'unknown';
    const endIdx = rest.indexOf(IM_END);
    const complete = endIdx >= 0;
    // Chat templates end a prompt by opening an assistant turn for generation.
    // It has no IM_END because it is not a completed transcript turn.
    const terminalGenerationPrompt =
      index === parts.length - 1 && role === 'assistant' && isGenerationPrompt(rest);
    if (!complete && !terminalGenerationPrompt) {
      pushWarning(warnings, 'incomplete_turn');
    }
    const payload = complete ? rest.slice(0, endIdx) : rest;
    return { role, payload, complete };
  });

  return { prefix, turns };
}

function extractTools(systemPayload: string): { tools: ToolDef[]; systemPrompt: string } {
  const tools: ToolDef[] = [];
  const toolsMatch = systemPayload.match(/<tools>\n?([\s\S]*?)\n?<\/tools>/);
  if (toolsMatch) {
    for (const line of toolsMatch[1].split('\n')) {
      const trimmed = line.trim();
      if (!trimmed) {
        continue;
      }
      try {
        const def = JSON.parse(trimmed) as ToolDef;
        if (def && typeof def.name === 'string') {
          tools.push(def);
        }
      } catch {
        // Ignore malformed tool definition lines.
      }
    }
  }

  const importantIdx = systemPayload.lastIndexOf('</IMPORTANT>');
  let systemPrompt = systemPayload;
  if (importantIdx >= 0) {
    systemPrompt = systemPayload.slice(importantIdx + '</IMPORTANT>'.length).trim();
  } else if (toolsMatch) {
    systemPrompt = systemPayload.slice(toolsMatch.index! + toolsMatch[0].length).trim();
  }

  return { tools, systemPrompt };
}

function parseXmlToolCall(block: string, warnings: ParserWarning[]): ToolCall {
  const functionMatch = block.match(/<function=([^>\n]+)>/);
  if (!functionMatch) {
    pushWarning(warnings, 'unparsed_tool_call');
    return { name: 'unknown', arguments: {}, raw: block, format: 'unknown' };
  }

  const name = functionMatch[1].trim();
  const args: Record<string, string> = {};
  const paramRe = /<parameter=([^>\n]+)>\n?([\s\S]*?)\n?<\/parameter>/g;
  let match: RegExpExecArray | null;
  while ((match = paramRe.exec(block)) !== null) {
    args[match[1].trim()] = match[2];
  }

  return { name, arguments: args, raw: block, format: 'qwen35_xml' };
}

function parseJsonToolCall(block: string, warnings: ParserWarning[]): ToolCall {
  try {
    const parsed = JSON.parse(block.trim()) as {
      name?: string;
      arguments?: Record<string, unknown>;
    };
    const args: Record<string, string> = {};
    if (parsed.arguments && typeof parsed.arguments === 'object') {
      for (const [key, value] of Object.entries(parsed.arguments)) {
        args[key] = typeof value === 'string' ? value : JSON.stringify(value);
      }
    }
    return {
      name: parsed.name ?? 'unknown',
      arguments: args,
      raw: block,
      format: 'qwen3_json',
    };
  } catch {
    pushWarning(warnings, 'unparsed_tool_call');
    return { name: 'unknown', arguments: {}, raw: block, format: 'unknown' };
  }
}

function parseToolCalls(
  text: string,
  warnings: ParserWarning[]
): { calls: ToolCall[]; preamble: string; tail: string } {
  const calls: ToolCall[] = [];
  const firstCall = text.indexOf(TOOL_CALL_OPEN);
  if (firstCall < 0) {
    if (text.includes(TOOL_CALL_OPEN)) {
      pushWarning(warnings, 'unparsed_tool_call');
    }
    return { calls, preamble: text.trim(), tail: '' };
  }

  const preamble = text.slice(0, firstCall).trim();
  const re = /<tool_call>([\s\S]*?)(?:<\/tool_call>|$)/g;
  let match: RegExpExecArray | null;
  let lastIndex = firstCall;

  // Start scanning from the first tool call.
  const toolSection = text.slice(firstCall);
  const localRe = /<tool_call>([\s\S]*?)(?:<\/tool_call>|$)/g;
  while ((match = localRe.exec(toolSection)) !== null) {
    lastIndex = firstCall + localRe.lastIndex;
    const inner = match[1];
    if (!match[0].includes(TOOL_CALL_CLOSE)) {
      pushWarning(warnings, 'incomplete_turn');
    }
    if (inner.includes('<function=')) {
      calls.push(parseXmlToolCall(inner, warnings));
    } else if (inner.trim().startsWith('{')) {
      calls.push(parseJsonToolCall(inner, warnings));
    } else {
      pushWarning(warnings, 'unknown_tool_format');
      calls.push({ name: 'unknown', arguments: {}, raw: inner, format: 'unknown' });
    }
  }

  void re;
  const tail = text.slice(lastIndex).trim();
  return { calls, preamble, tail };
}

function extractToolResponses(payload: string): ToolResult[] {
  const results: ToolResult[] = [];
  const re = /<tool_response>\n?([\s\S]*?)\n?<\/tool_response>/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(payload)) !== null) {
    const content = match[1];
    results.push({
      content,
      isError: content.trimStart().startsWith('Error:'),
    });
  }
  return results;
}

function isGenerationPrompt(payload: string): boolean {
  const trimmed = payload.trim();
  return (
    trimmed === '' ||
    trimmed === THINK_OPEN ||
    trimmed === `${THINK_OPEN}\n` ||
    trimmed === `${THINK_OPEN}\n\n${THINK_CLOSE}` ||
    trimmed === `${THINK_OPEN}\n\n${THINK_CLOSE}\n` ||
    trimmed === `${THINK_OPEN}\n\n${THINK_CLOSE}\n\n`
  );
}

function parseAssistantPayload(payload: string, warnings: ParserWarning[]): TranscriptExchange | null {
  if (isGenerationPrompt(payload)) {
    return null;
  }

  let working = payload;
  let thinking: string | undefined;
  let incomplete = false;

  const openIdx = working.indexOf(THINK_OPEN);
  const closeIdx = working.indexOf(THINK_CLOSE);

  if (closeIdx >= 0) {
    const thinkStart = openIdx >= 0 && openIdx < closeIdx ? openIdx + THINK_OPEN.length : 0;
    thinking = working.slice(thinkStart, closeIdx).replace(/^\n+/, '').replace(/\n+$/, '');
    working = working.slice(closeIdx + THINK_CLOSE.length);
  } else if (openIdx >= 0) {
    thinking = working.slice(openIdx + THINK_OPEN.length).replace(/^\n+/, '');
    working = '';
    incomplete = true;
    pushWarning(warnings, 'truncated_thinking');
  }

  const { calls, preamble, tail } = parseToolCalls(working, warnings);

  if (!thinking && calls.length === 0 && !preamble && !tail) {
    return null;
  }

  return {
    thinking: thinking || undefined,
    preamble: calls.length > 0 ? preamble || undefined : undefined,
    toolCalls: calls,
    toolResults: [],
    assistantText: calls.length === 0 ? preamble || undefined : tail || undefined,
    incomplete: incomplete || undefined,
  };
}

function attachToolResults(
  exchanges: TranscriptExchange[],
  results: ToolResult[],
  warnings: ParserWarning[]
): void {
  if (results.length === 0) {
    return;
  }

  for (let i = exchanges.length - 1; i >= 0; i--) {
    const exchange = exchanges[i];
    if (exchange.toolCalls.length > exchange.toolResults.length) {
      exchange.toolResults.push(...results);
      return;
    }
  }

  for (let i = exchanges.length - 1; i >= 0; i--) {
    if (exchanges[i].toolCalls.length > 0) {
      exchanges[i].toolResults.push(...results);
      return;
    }
  }

  pushWarning(warnings, 'unmatched_tool_result');
  exchanges.push({
    toolCalls: [],
    toolResults: results,
  });
}

function buildTurnSequence(prompt: string, response: string, warnings: ParserWarning[]): RawTurn[] {
  const promptTurns = splitTurns(prompt, warnings).turns;
  const responseSplit = splitTurns(response, warnings);
  const responseTurns = responseSplit.turns;
  const responsePrefix = responseSplit.prefix;

  if (responseTurns.length === 0 && !responsePrefix) {
    return promptTurns;
  }

  // Response has no role markers: treat entire response as one assistant continuation.
  if (!response.includes(IM_START)) {
    const lastPrompt = promptTurns[promptTurns.length - 1];
    const responsePayload = response.includes(IM_END)
      ? response.slice(0, response.indexOf(IM_END))
      : response;
    if (lastPrompt?.role === 'assistant' && isGenerationPrompt(lastPrompt.payload)) {
      return [
        ...promptTurns.slice(0, -1),
        {
          role: 'assistant',
          payload: `${lastPrompt.payload}${responsePayload}`,
          complete: response.includes(IM_END),
        },
      ];
    }
    return [
      ...promptTurns,
      {
        role: 'assistant',
        payload: responsePayload,
        complete: response.includes(IM_END),
      },
    ];
  }

  // Response starts mid-assistant-turn, then continues with more <|im_start|> turns.
  const prefixComplete = responsePrefix.includes(IM_END);
  const prefixPayload = prefixComplete
    ? responsePrefix.slice(0, responsePrefix.indexOf(IM_END))
    : responsePrefix;
  const lastPrompt = promptTurns[promptTurns.length - 1];

  if (responsePrefix.trim()) {
    if (lastPrompt?.role === 'assistant' && isGenerationPrompt(lastPrompt.payload)) {
      return [
        ...promptTurns.slice(0, -1),
        {
          role: 'assistant',
          payload: `${lastPrompt.payload}${prefixPayload}`,
          complete: prefixComplete,
        },
        ...responseTurns,
      ];
    }

    return [
      ...promptTurns,
      { role: 'assistant', payload: prefixPayload, complete: prefixComplete },
      ...responseTurns,
    ];
  }

  return [...promptTurns, ...responseTurns];
}

export function parseTrajectory(rawBody: string): ParsedTrajectory {
  const { body, warnings } = parseBody(rawBody);
  const turns = buildTurnSequence(body.prompt, body.response, warnings);

  let systemPrompt = '';
  let tools: ToolDef[] = [];
  let userPrompt = '';
  const exchanges: TranscriptExchange[] = [];

  for (const turn of turns) {
    if (turn.role === 'system') {
      const extracted = extractTools(turn.payload);
      tools = extracted.tools;
      systemPrompt = extracted.systemPrompt || turn.payload.trim();
      continue;
    }

    if (turn.role === 'user') {
      const toolResults = extractToolResponses(turn.payload);
      if (toolResults.length > 0) {
        attachToolResults(exchanges, toolResults, warnings);
      } else if (!userPrompt) {
        userPrompt = turn.payload.trim();
      }
      continue;
    }

    if (turn.role === 'assistant') {
      const exchange = parseAssistantPayload(turn.payload, warnings);
      if (!exchange) {
        continue;
      }
      if (!turn.complete) {
        exchange.incomplete = true;
      }
      exchanges.push(exchange);
    }
  }

  return {
    systemPrompt,
    tools,
    userPrompt,
    exchanges,
    warnings,
    rawPrompt: body.prompt,
    rawResponse: body.response,
  };
}
