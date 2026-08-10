import React, { useEffect, useMemo, useState } from 'react';
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

type SortKey =
  | 'time'
  | 'taskId'
  | 'groupId'
  | 'rolloutIndex'
  | 'reward'
  | 'numTurns'
  | 'doneReason'
  | 'promptTokens'
  | 'responseTokens';
type SortDirection = 'asc' | 'desc';

const numericSortKeys = new Set<SortKey>(['rolloutIndex', 'reward', 'numTurns', 'promptTokens', 'responseTokens']);

function compareRows(left: TrajectoryRow, right: TrajectoryRow, key: SortKey): number {
  const leftValue = left[key];
  const rightValue = right[key];
  if (leftValue == null) {
    return rightValue == null ? 0 : 1;
  }
  if (rightValue == null) {
    return -1;
  }
  if (key === 'time') {
    return new Date(leftValue).getTime() - new Date(rightValue).getTime();
  }
  if (numericSortKeys.has(key)) {
    return Number(leftValue) - Number(rightValue);
  }
  return String(leftValue).localeCompare(String(rightValue));
}

export const TrajectoriesPanel: React.FC<Props> = ({ options, data, width, height, fieldConfig, id }) => {
  const styles = useStyles2(getStyles);
  const [selected, setSelected] = useState<SelectedTrace | null>(null);
  const [page, setPage] = useState(0);
  const [sortKey, setSortKey] = useState<SortKey>('time');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');

  const rows = useMemo(() => {
    const frame = data.series[0];
    return frame ? rowsFromFrame(frame) : [];
  }, [data.series]);

  if (data.series.length === 0 || rows.length === 0) {
    return <PanelDataErrorView fieldConfig={fieldConfig} panelId={id} data={data} needsStringField />;
  }

  const previewLen = options.bodyPreviewLength > 0 ? options.bodyPreviewLength : 60;
  const pageSize = Math.max(1, Math.floor(options.pageSize || 25));
  const sortedRows = useMemo(
    () =>
      [...rows].sort((left, right) => {
        const comparison = compareRows(left, right, sortKey);
        return sortDirection === 'asc' ? comparison : -comparison;
      }),
    [rows, sortDirection, sortKey]
  );
  const pageCount = Math.max(1, Math.ceil(sortedRows.length / pageSize));
  const currentPage = Math.min(page, pageCount - 1);
  const pageRows = sortedRows.slice(currentPage * pageSize, (currentPage + 1) * pageSize);

  useEffect(() => {
    setPage(0);
  }, [rows.length, sortDirection, sortKey]);

  const changeSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc');
      return;
    }
    setSortKey(key);
    setSortDirection(key === 'time' ? 'desc' : 'asc');
  };

  const sortableHeader = (label: string, key: SortKey) => {
    const active = key === sortKey;
    const arrow = active ? (sortDirection === 'asc' ? ' ▲' : ' ▼') : '';
    return (
      <th aria-sort={active ? (sortDirection === 'asc' ? 'ascending' : 'descending') : 'none'}>
        <button className={styles.sortButton} onClick={() => changeSort(key)} type="button">
          {label}
          {arrow}
        </button>
      </th>
    );
  };

  const openTrace = (row: TrajectoryRow) => {
    setSelected({
      row,
      parsed: parseTrajectory(row.body),
    });
  };

  const selectedIndex = selected ? sortedRows.findIndex((row) => row.index === selected.row.index) : -1;

  const selectTraceAt = (index: number) => {
    const row = sortedRows[index];
    if (row) {
      openTrace(row);
    }
  };

  return (
    <div className={styles.wrapper} style={{ width, height }} data-testid="trajectories-panel">
      <div className={styles.header}>
        <Button variant="secondary" size="sm" onClick={() => selectTraceAt(0)} data-testid="view-trajectories-button">
          View Trajectories
        </Button>
      </div>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              {sortableHeader('Time', 'time')}
              {sortableHeader('TaskId', 'taskId')}
              {sortableHeader('GroupId', 'groupId')}
              {sortableHeader('Rollout', 'rolloutIndex')}
              {sortableHeader('Reward', 'reward')}
              {sortableHeader('Turns', 'numTurns')}
              {sortableHeader('DoneReason', 'doneReason')}
              {sortableHeader('PromptTok', 'promptTokens')}
              {sortableHeader('RespTok', 'responseTokens')}
              <th>Body</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) => (
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
                <td className={styles.bodyCell} onClick={() => openTrace(row)} data-testid="trace-body-cell">
                  <Button
                    variant="secondary"
                    fill="text"
                    size="sm"
                    onClick={(event) => {
                      event.stopPropagation();
                      openTrace(row);
                    }}
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
      <div className={styles.pagination}>
        <span>{`${currentPage * pageSize + 1}–${Math.min((currentPage + 1) * pageSize, rows.length)} of ${rows.length}`}</span>
        <Button variant="secondary" size="sm" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>
          Previous
        </Button>
        <span>
          Page {currentPage + 1} of {pageCount}
        </span>
        <Button
          variant="secondary"
          size="sm"
          disabled={currentPage >= pageCount - 1}
          onClick={() => setPage(currentPage + 1)}
        >
          Next
        </Button>
      </div>

      {selected && (
        <TraceModal
          row={selected.row}
          parsed={selected.parsed}
          tracePosition={selectedIndex + 1}
          traceCount={sortedRows.length}
          onDismiss={() => setSelected(null)}
          onPrevious={() => selectTraceAt(selectedIndex - 1)}
          onNext={() => selectTraceAt(selectedIndex + 1)}
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
  header: css({
    display: 'flex',
    justifyContent: 'flex-end',
    padding: theme.spacing(1),
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
  pagination: css({
    alignItems: 'center',
    borderTop: `1px solid ${theme.colors.border.weak}`,
    display: 'flex',
    gap: theme.spacing(1),
    justifyContent: 'flex-end',
    padding: theme.spacing(1),
  }),
  sortButton: css({
    background: 'none',
    border: 0,
    color: 'inherit',
    cursor: 'pointer',
    font: 'inherit',
    fontWeight: 'inherit',
    padding: 0,
  }),
  bodyCell: css({
    cursor: 'pointer',
  }),
});
