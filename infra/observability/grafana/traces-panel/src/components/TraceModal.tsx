import React from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2 } from '@grafana/data';
import { Alert, Badge, Button, Collapse, Modal, Stack, Text, useStyles2 } from '@grafana/ui';
import { ParsedTrajectory, ToolCall, TranscriptExchange } from '../parsing';
import { TrajectoryRow, formatTime } from './rows';

interface Props {
  row: TrajectoryRow;
  parsed: ParsedTrajectory;
  tracePosition: number;
  traceCount: number;
  onDismiss: () => void;
  onPrevious: () => void;
  onNext: () => void;
}

function formatArgs(call: ToolCall): string {
  return Object.entries(call.arguments)
    .map(([key, value]) => `${key}=\n${value}`)
    .join('\n\n');
}

export const TraceModal: React.FC<Props> = ({
  row,
  parsed,
  tracePosition,
  traceCount,
  onDismiss,
  onPrevious,
  onNext,
}) => {
  const styles = useStyles2(getStyles);
  const title = [
    row.taskId ? `Task ${row.taskId}` : 'Trajectory',
    row.rolloutIndex != null ? `rollout ${row.rolloutIndex}` : null,
    row.doneReason ? row.doneReason : null,
  ]
    .filter(Boolean)
    .join(' · ');

  return (
    <Modal title={title} isOpen onDismiss={onDismiss} className={styles.modal} contentClassName={styles.modalContent}>
      <div data-testid="trace-modal" className={styles.body}>
        <div className={styles.meta}>
          <Text variant="bodySmall" color="secondary">
            {formatTime(row.time)}
            {row.numTurns != null ? ` · ${row.numTurns} turns` : ''}
            {row.promptTokens != null || row.responseTokens != null
              ? ` · tokens ${row.promptTokens ?? '—'}/${row.responseTokens ?? '—'}`
              : ''}
          </Text>
          <Text element="p" weight="medium" data-testid="trace-reward">
            Reward: {row.reward ?? '—'}
          </Text>
        </div>

        {parsed.warnings.length > 0 && (
          <Alert title="Parser warnings" severity="warning" data-testid="trace-warnings">
            {parsed.warnings.join(', ')}
          </Alert>
        )}

        <section className={styles.section} data-testid="trace-system-prompt">
          <Text element="h3">System prompt</Text>
          <pre className={styles.pre}>{parsed.systemPrompt || '(empty)'}</pre>
          {parsed.tools.length > 0 && (
            <div className={styles.tools}>
              <Text variant="bodySmall" color="secondary">
                Tools:{' '}
                {parsed.tools.map((t) => (
                  <Badge key={t.name} text={t.name} color="blue" className={styles.badge} />
                ))}
              </Text>
            </div>
          )}
        </section>

        <section className={styles.section} data-testid="trace-user-prompt">
          <Text element="h3">User task</Text>
          <pre className={styles.pre}>{parsed.userPrompt || '(empty)'}</pre>
        </section>

        <section className={styles.section}>
          <Text element="h3">Agent transcript</Text>
          <div className={styles.transcript} data-testid="trace-transcript">
            {parsed.exchanges.length === 0 && (
              <Text color="secondary">No assistant turns found in this trajectory body.</Text>
            )}
            {parsed.exchanges.map((exchange, idx) => (
              <ExchangeRow key={idx} exchange={exchange} index={idx} />
            ))}
          </div>
        </section>
      </div>
      <Modal.ButtonRow
        leftItems={
          <Text color="secondary">
            Trace {tracePosition} of {traceCount}
          </Text>
        }
      >
        <Button
          variant="secondary"
          size="sm"
          disabled={tracePosition === 1}
          onClick={onPrevious}
          data-testid="previous-trace-button"
        >
          Previous trace
        </Button>
        <Button
          variant="secondary"
          size="sm"
          disabled={tracePosition === traceCount}
          onClick={onNext}
          data-testid="next-trace-button"
        >
          Next trace
        </Button>
      </Modal.ButtonRow>
    </Modal>
  );
};

const ExchangeRow: React.FC<{ exchange: TranscriptExchange; index: number }> = ({ exchange, index }) => {
  const styles = useStyles2(getStyles);
  const missingResponse = exchange.toolCalls.length > 0 && exchange.toolResults.length === 0;

  return (
    <div className={styles.exchange} data-testid={`trace-exchange-${index}`}>
      <div className={styles.left}>
        <div className={styles.bubbleLabel}>Agent</div>
        {exchange.thinking && (
          <Collapse label="Thinking" isOpen={false}>
            <pre className={styles.pre}>{exchange.thinking}</pre>
          </Collapse>
        )}
        {exchange.preamble && <pre className={styles.pre}>{exchange.preamble}</pre>}
        {exchange.toolCalls.map((call, callIdx) => (
          <div key={callIdx} className={styles.toolCall} data-testid="trace-tool-call">
            <Stack direction="row" gap={1} alignItems="center">
              <Badge text={call.name} color="purple" />
              <Text variant="bodySmall" color="secondary">
                {call.format}
              </Text>
            </Stack>
            <pre className={styles.pre}>{formatArgs(call) || call.raw}</pre>
          </div>
        ))}
        {exchange.assistantText && (
          <pre className={styles.pre} data-testid="trace-assistant-text">
            {exchange.assistantText}
          </pre>
        )}
        {exchange.incomplete && <Badge text="incomplete" color="orange" />}
      </div>

      <div className={styles.right}>
        <div className={styles.bubbleLabel}>Tool</div>
        {exchange.toolResults.map((result, resultIdx) => (
          <div
            key={resultIdx}
            className={result.isError ? styles.toolError : styles.toolResult}
            data-testid="trace-tool-result"
          >
            {result.isError && <Badge text="error" color="red" />}
            <pre className={styles.pre}>{result.content}</pre>
          </div>
        ))}
        {missingResponse && (
          <Text color="secondary" data-testid="trace-missing-response">
            No tool response in this trajectory body.
          </Text>
        )}
        {exchange.toolCalls.length === 0 && exchange.toolResults.length === 0 && <Text color="secondary">—</Text>}
      </div>
    </div>
  );
};

const getStyles = (theme: GrafanaTheme2) => ({
  modal: css({
    width: 'min(1200px, 96vw)',
    maxWidth: '96vw',
  }),
  modalContent: css({
    maxHeight: '80vh',
    overflow: 'auto',
  }),
  body: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(2),
  }),
  meta: css({
    marginBottom: theme.spacing(1),
  }),
  section: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(1),
  }),
  tools: css({
    display: 'flex',
    flexWrap: 'wrap',
    gap: theme.spacing(0.5),
  }),
  badge: css({
    marginRight: theme.spacing(0.5),
  }),
  pre: css({
    margin: 0,
    padding: theme.spacing(1),
    background: theme.colors.background.secondary,
    border: `1px solid ${theme.colors.border.weak}`,
    borderRadius: theme.shape.radius.default,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontFamily: theme.typography.fontFamilyMonospace,
    fontSize: theme.typography.bodySmall.fontSize,
    maxHeight: 320,
    overflow: 'auto',
  }),
  transcript: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(2),
  }),
  exchange: css({
    display: 'grid',
    gridTemplateColumns: '1fr 1fr',
    gap: theme.spacing(2),
    [theme.breakpoints.down('md')]: {
      gridTemplateColumns: '1fr',
    },
  }),
  left: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(1),
    padding: theme.spacing(1.5),
    background: theme.colors.background.canvas,
    borderRadius: theme.shape.radius.default,
    border: `1px solid ${theme.colors.border.weak}`,
  }),
  right: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(1),
    padding: theme.spacing(1.5),
    background: theme.colors.background.secondary,
    borderRadius: theme.shape.radius.default,
    border: `1px solid ${theme.colors.border.weak}`,
  }),
  bubbleLabel: css({
    fontSize: theme.typography.bodySmall.fontSize,
    color: theme.colors.text.secondary,
    fontWeight: theme.typography.fontWeightMedium,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
  }),
  toolCall: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(0.5),
  }),
  toolResult: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(0.5),
  }),
  toolError: css({
    display: 'flex',
    flexDirection: 'column',
    gap: theme.spacing(0.5),
    borderLeft: `3px solid ${theme.colors.error.main}`,
    paddingLeft: theme.spacing(1),
  }),
});
