import React, { useMemo, useState } from 'react';
import { GrafanaTheme2, PanelProps } from '@grafana/data';
import { css } from '@emotion/css';
import { Button, useStyles2 } from '@grafana/ui';
import { PanelDataErrorView } from '@grafana/runtime';
import { TrajectoriesOptions } from '../types';
import { parseTrajectory, ParsedTrajectory } from '../parsing';
import { TraceModal } from './TraceModal';
import { formatTime, rowsFromFrame, TrajectoryRow, truncate } from './rows';

interface Props extends PanelProps<TrajectoriesOptions> {}

interface SelectedTrace {
  row: TrajectoryRow;
  parsed: ParsedTrajectory;
}

export const TrajectoriesPanel: React.FC<Props> = ({ options, data, width, height, fieldConfig, id }) => {
  const styles = useStyles2(getStyles);
  const [selected, setSelected] = useState<SelectedTrace | null>(null);

  const rows = useMemo(() => {
    const frame = data.series[0];
    return frame ? rowsFromFrame(frame) : [];
  }, [data.series]);

  if (data.series.length === 0 || rows.length === 0) {
    return <PanelDataErrorView fieldConfig={fieldConfig} panelId={id} data={data} needsStringField />;
  }

  const previewLen = options.bodyPreviewLength > 0 ? options.bodyPreviewLength : 60;

  const openTrace = (row: TrajectoryRow) => {
    setSelected({
      row,
      parsed: parseTrajectory(row.body),
    });
  };

  return (
    <div
      className={styles.wrapper}
      style={{ width, height }}
      data-testid="trajectories-panel"
    >
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Time</th>
              <th>TaskId</th>
              <th>GroupId</th>
              <th>Rollout</th>
              <th>Reward</th>
              <th>Turns</th>
              <th>DoneReason</th>
              <th>PromptTok</th>
              <th>RespTok</th>
              <th>Body</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.index} data-testid="trajectory-row">
                <td>{formatTime(row.time)}</td>
                <td title={row.taskId}>{truncate(row.taskId ?? '—', 24)}</td>
                <td title={row.groupId}>{truncate(row.groupId ?? '—', 16)}</td>
                <td>{row.rolloutIndex ?? '—'}</td>
                <td>{row.reward != null ? Number(row.reward).toFixed(3) : '—'}</td>
                <td>{row.numTurns ?? '—'}</td>
                <td>{row.doneReason ?? '—'}</td>
                <td>{row.promptTokens ?? '—'}</td>
                <td>{row.responseTokens ?? '—'}</td>
                <td>
                  <Button
                    variant="secondary"
                    fill="text"
                    size="sm"
                    onClick={() => openTrace(row)}
                    data-testid="view-trace-button"
                    aria-label={`View trace for ${row.taskId ?? `row ${row.index}`}`}
                  >
                    {row.body ? truncate(row.body.replace(/\s+/g, ' '), previewLen) : 'View trace'}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <TraceModal
          row={selected.row}
          parsed={selected.parsed}
          onDismiss={() => setSelected(null)}
        />
      )}
    </div>
  );
};

const getStyles = (theme: GrafanaTheme2) => ({
  wrapper: css({
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
    fontFamily: theme.typography.fontFamily,
  }),
  tableWrap: css({
    flex: 1,
    overflow: 'auto',
  }),
  table: css({
    width: '100%',
    borderCollapse: 'collapse',
    fontSize: theme.typography.bodySmall.fontSize,
    'th, td': {
      textAlign: 'left',
      padding: `${theme.spacing(0.75)} ${theme.spacing(1)}`,
      borderBottom: `1px solid ${theme.colors.border.weak}`,
      whiteSpace: 'nowrap',
      verticalAlign: 'middle',
    },
    th: {
      position: 'sticky',
      top: 0,
      background: theme.colors.background.primary,
      zIndex: 1,
      fontWeight: theme.typography.fontWeightMedium,
    },
    'tbody tr:hover': {
      background: theme.colors.action.hover,
    },
  }),
});
