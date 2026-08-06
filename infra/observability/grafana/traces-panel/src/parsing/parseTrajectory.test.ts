import * as fs from 'fs';
import * as path from 'path';
import { parseTrajectory } from './parseTrajectory';

const SAMPLE_SYSTEM = `<|im_start|>system
# Tools

You have access to the following functions:

<tools>
{"name": "bash", "description": "Run a bash command", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}
{"name": "submit", "description": "Submit solution", "parameters": {"type": "object", "properties": {}}}
</tools>

If you choose to call a function ONLY reply in the following format with NO suffix:

<tool_call>
<function=example_function_name>
<parameter=example_parameter_1>
value_1
</parameter>
</function>
</tool_call>

<IMPORTANT>
Reminder:
- Function calls MUST follow the specified format
</IMPORTANT>

You are an autonomous software engineer working inside a checked-out Git repository.<|im_end|>
`;

function body(prompt: string, response: string): string {
  return JSON.stringify({ prompt, response });
}

describe('parseTrajectory', () => {
  it('parses malformed body JSON with a warning', () => {
    const parsed = parseTrajectory('{not-json');
    expect(parsed.warnings).toContain('malformed_body_json');
    expect(parsed.exchanges).toEqual([]);
  });

  it('parses system prompt, user prompt, thinking, and qwen35 XML tool call', () => {
    const prompt =
      SAMPLE_SYSTEM +
      `<|im_start|>user
Fix the golden-section bug.<|im_end|>
<|im_start|>assistant
<think>
`;
    const response = `I should inspect the repo.
</think>

<tool_call>
<function=bash>
<parameter=command>
find /testbed -type f -name "*.py" | head -20
</parameter>
</function>
</tool_call><|im_end|>`;

    const parsed = parseTrajectory(body(prompt, response));
    expect(parsed.systemPrompt).toContain('autonomous software engineer');
    expect(parsed.tools.map((t) => t.name)).toEqual(['bash', 'submit']);
    expect(parsed.userPrompt).toBe('Fix the golden-section bug.');
    expect(parsed.exchanges).toHaveLength(1);
    expect(parsed.exchanges[0].thinking).toContain('inspect the repo');
    expect(parsed.exchanges[0].toolCalls).toHaveLength(1);
    expect(parsed.exchanges[0].toolCalls[0]).toMatchObject({
      name: 'bash',
      format: 'qwen35_xml',
      arguments: {
        command: 'find /testbed -type f -name "*.py" | head -20',
      },
    });
  });

  it('pairs tool responses with the preceding tool call', () => {
    const prompt =
      SAMPLE_SYSTEM +
      `<|im_start|>user
Do something.<|im_end|>
<|im_start|>assistant
<think>
`;
    const response = `plan
</think>
<tool_call>
<function=bash>
<parameter=command>
ls
</parameter>
</function>
</tool_call><|im_end|>
<|im_start|>user
<tool_response>
file_a.py
file_b.py
</tool_response><|im_end|>
<|im_start|>assistant
<think>
next
</think>
I will keep going.<|im_end|>`;

    const parsed = parseTrajectory(body(prompt, response));
    expect(parsed.exchanges).toHaveLength(2);
    expect(parsed.exchanges[0].toolResults).toEqual([
      { content: 'file_a.py\nfile_b.py', isError: false },
    ]);
    expect(parsed.exchanges[1].thinking).toBe('next');
    expect(parsed.exchanges[1].assistantText).toBe('I will keep going.');
  });

  it('flags tool errors and truncated thinking', () => {
    const prompt =
      SAMPLE_SYSTEM +
      `<|im_start|>user
x<|im_end|>
<|im_start|>assistant
<think>
`;

    const truncated = parseTrajectory(body(prompt, `still thinking without close`));
    expect(truncated.warnings).toContain('truncated_thinking');
    expect(truncated.exchanges[0].thinking).toContain('still thinking');

    const withError = parseTrajectory(
      body(
        SAMPLE_SYSTEM +
          `<|im_start|>user
x<|im_end|>
<|im_start|>assistant
<think>
`,
        `ok
</think>
<tool_call>
<function=bash>
<parameter=command>
boom
</parameter>
</function>
</tool_call><|im_end|>
<|im_start|>user
<tool_response>
Error: command failed
</tool_response><|im_end|>`
      )
    );
    expect(withError.exchanges[0].toolResults[0].isError).toBe(true);
  });

  it('parses multiline parameter values', () => {
    const parsed = parseTrajectory(
      body(
        SAMPLE_SYSTEM +
          `<|im_start|>user
x<|im_end|>
`,
        `<think>
t
</think>
<tool_call>
<function=bash>
<parameter=command>
echo line1
echo line2
</parameter>
</function>
</tool_call><|im_end|>`
      )
    );
    expect(parsed.exchanges[0].toolCalls[0].arguments.command).toBe('echo line1\necho line2');
  });

  it('does not flag the terminal assistant generation prompt as an incomplete turn', () => {
    const prompt =
      '<|im_start|>user\nReverse the string: ioy\nRespond with the reversed string in <answer>...</answer> tags.<|im_end|>\n<|im_start|>assistant\n';
    const response =
      'The reverse of the string "ioy" is "yoi".\n\n<answer>yoi</answer><|im_end|>';

    const parsed = parseTrajectory(body(prompt, response));

    expect(parsed.warnings).not.toContain('incomplete_turn');
    expect(parsed.userPrompt).toContain('Reverse the string: ioy');
    expect(parsed.exchanges).toEqual([
      expect.objectContaining({
        assistantText: 'The reverse of the string "ioy" is "yoi".\n\n<answer>yoi</answer>',
      }),
    ]);
  });

  it('still flags a terminal assistant turn that contains incomplete content', () => {
    const parsed = parseTrajectory(
      body('<|im_start|>user\nx<|im_end|>\n<|im_start|>assistant\npartial answer', '')
    );

    expect(parsed.warnings).toContain('incomplete_turn');
  });

  it('parses sample.json-style truncated prompt without generation marker', () => {
    // Mirrors the checked-in sample: user turn never closed, response has </think> + tool_call.
    const prompt =
      SAMPLE_SYSTEM +
      `<|im_start|>user
golden-section search fails`;
    const response = `Looking at the bug.
</think>

<tool_call>
<function=bash>
<parameter=command>
find /testbed -type f -name "*.py" | head -20
</parameter>
</function>
</tool_call><|im_end|>`;

    const parsed = parseTrajectory(body(prompt, response));
    expect(parsed.userPrompt).toBe('golden-section search fails');
    expect(parsed.exchanges).toHaveLength(1);
    expect(parsed.exchanges[0].thinking).toContain('Looking at the bug');
    expect(parsed.exchanges[0].toolCalls[0].name).toBe('bash');
  });

  it('parses the checked-in sample.json fixture', () => {
    const samplePath = path.resolve(__dirname, '../../../../../../sample.json');
    const raw = fs.readFileSync(samplePath, 'utf8');
    const parsed = parseTrajectory(raw);

    expect(parsed.systemPrompt).toContain('autonomous software engineer');
    expect(parsed.tools.map((t) => t.name)).toEqual(['bash', 'submit']);
    expect(parsed.userPrompt).toContain('golden-section search fails');
    expect(parsed.exchanges).toHaveLength(1);
    expect(parsed.exchanges[0].thinking).toContain('pvlib');
    expect(parsed.exchanges[0].toolCalls[0]).toMatchObject({
      name: 'bash',
      format: 'qwen35_xml',
    });
    expect(parsed.exchanges[0].toolCalls[0].arguments.command).toContain('find /testbed');
  });
});
