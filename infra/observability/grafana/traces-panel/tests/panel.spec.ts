import { test, expect } from '@grafana/plugin-e2e';

test('should display "No data" when the panel query is empty', async ({
  gotoPanelEditPage,
  readProvisionedDashboard,
}) => {
  const dashboard = await readProvisionedDashboard({ fileName: 'dashboard.json' });
  const panelEditPage = await gotoPanelEditPage({ dashboard, id: '2' });
  await expect(panelEditPage.panel.locator).toContainText('No data');
});

test('should render trajectory rows and open the trace modal from the panel control', async ({
  gotoPanelEditPage,
  readProvisionedDashboard,
  page,
}) => {
  const dashboard = await readProvisionedDashboard({ fileName: 'dashboard.json' });
  const panelEditPage = await gotoPanelEditPage({ dashboard, id: '1' });

  await expect(page.getByTestId('trajectories-panel')).toBeVisible();
  await expect(page.getByTestId('trajectory-row').first()).toBeVisible();

  await page.getByTestId('view-trajectories-button').click();

  const modal = page.getByTestId('trace-modal');
  await expect(modal).toBeVisible();
  await expect(page.getByTestId('trace-system-prompt')).toContainText('autonomous software engineer');
  await expect(page.getByTestId('trace-user-prompt')).toBeVisible();
  await expect(page.getByTestId('trace-tool-call').first()).toBeVisible();
  await expect(page.getByTestId('trace-reward')).toContainText('Reward:');
  await expect(page.getByTestId('previous-trace-button')).toBeDisabled();
  await expect(page.getByTestId('next-trace-button')).toBeEnabled();

  await page.getByTestId('next-trace-button').click();
  await expect(page.getByTestId('trace-transcript')).toContainText('README.md');
  await expect(page.getByTestId('previous-trace-button')).toBeEnabled();

  // Close via the modal dismiss control (X).
  await page.getByRole('button', { name: /close/i }).click();
  await expect(modal).toHaveCount(0);
});

test('should show tool responses on the right for multi-turn fixtures', async ({
  gotoPanelEditPage,
  readProvisionedDashboard,
  page,
}) => {
  const dashboard = await readProvisionedDashboard({ fileName: 'dashboard.json' });
  await gotoPanelEditPage({ dashboard, id: '1' });

  // Second provisioned row is the multi-turn fixture with a tool response.
  await page.getByTestId('view-trace-button').nth(1).click();

  await expect(page.getByTestId('trace-modal')).toBeVisible();
  await expect(page.getByTestId('trace-tool-call').first()).toContainText('bash');
  await expect(page.getByTestId('trace-tool-result').first()).toContainText('README.md');
  await expect(page.getByTestId('trace-transcript')).toBeVisible();
});
