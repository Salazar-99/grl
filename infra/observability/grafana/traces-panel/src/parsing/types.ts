export interface ToolDef {
  name: string;
  description: string;
  parameters: unknown;
}

export interface ToolCall {
  name: string;
  arguments: Record<string, string>;
  raw: string;
  format: 'qwen35_xml' | 'qwen3_json' | 'unknown';
}

export interface ToolResult {
  content: string;
  isError: boolean;
}

export interface TranscriptExchange {
  thinking?: string;
  preamble?: string;
  toolCalls: ToolCall[];
  toolResults: ToolResult[];
  assistantText?: string;
  incomplete?: boolean;
}

export type ParserWarning =
  | 'malformed_body_json'
  | 'incomplete_turn'
  | 'truncated_thinking'
  | 'unparsed_tool_call'
  | 'unknown_tool_format'
  | 'unmatched_tool_result';

export interface ParsedTrajectory {
  systemPrompt: string;
  tools: ToolDef[];
  userPrompt: string;
  exchanges: TranscriptExchange[];
  warnings: ParserWarning[];
  rawPrompt: string;
  rawResponse: string;
}

export interface TrajectoryBody {
  prompt: string;
  response: string;
}
