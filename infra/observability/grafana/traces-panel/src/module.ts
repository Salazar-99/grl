import { PanelPlugin } from '@grafana/data';
import { TrajectoriesOptions } from './types';
import { TrajectoriesPanel } from './components/TrajectoriesPanel';

export const plugin = new PanelPlugin<TrajectoriesOptions>(TrajectoriesPanel).setPanelOptions((builder) => {
  return builder.addNumberInput({
    path: 'bodyPreviewLength',
    name: 'Body preview length',
    description: 'Maximum characters shown in the Body column before opening the trace modal',
    defaultValue: 60,
  });
});
