/**
 * The Telegram plate-clear button setting.
 *
 * - It only shows once the plate-clear notification is on, and only for Telegram.
 * - The chosen value reaches the API, and another provider type always sends false.
 * - The provider card offers the same toggle under the same conditions.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { AddNotificationModal } from '../../components/AddNotificationModal';
import { NotificationProviderCard } from '../../components/NotificationProviderCard';
import type { NotificationProvider } from '../../api/client';

const BUTTON_LABEL = 'Plate-clear button';

afterEach(() => {
  server.resetHandlers();
  vi.restoreAllMocks();
});

function buildProvider(overrides: Partial<NotificationProvider> = {}): NotificationProvider {
  return {
    id: 1,
    name: 'My Telegram',
    provider_type: 'telegram',
    enabled: true,
    config: { bot_token: '123:abc', chat_id: '-100123' },
    on_print_complete: true,
    on_plate_clear_required: true,
    plate_clear_button: false,
    on_print_confirm_request: true,
    telegram_verdict_mode: 'buttons',
    quiet_hours_enabled: false,
    quiet_hours_start: null,
    quiet_hours_end: null,
    daily_digest_enabled: false,
    daily_digest_time: null,
    printer_id: null,
    last_success: null,
    last_error: null,
    last_error_at: null,
    created_at: '2026-09-20T00:00:00Z',
    updated_at: '2026-09-20T00:00:00Z',
    ...overrides,
  } as NotificationProvider;
}

function toggleFor(label: string): HTMLElement {
  const row = screen.getByText(label).closest('div.flex') as HTMLElement;
  return within(row).getByRole('switch');
}

function capturePatch() {
  const captured: { body: unknown } = { body: null };
  server.use(
    http.patch('*/api/v1/notifications/1', async ({ request }) => {
      captured.body = await request.json();
      return HttpResponse.json({ id: 1 });
    }),
  );
  return captured;
}

describe('AddNotificationModal — plate-clear button', () => {
  it('shows for Telegram with the event on, with the bot warning and the required setting', async () => {
    render(<AddNotificationModal provider={buildProvider()} onClose={() => undefined} />);

    expect(await screen.findByText(BUTTON_LABEL)).toBeInTheDocument();
    expect(screen.getByText(/polls this bot for taps, so use a bot only Bambuddy uses/)).toBeInTheDocument();
    expect(screen.getByText(/Only sent while .Require plate-clear confirmation. is on/)).toBeInTheDocument();
  });

  it('stays hidden while the event is off', async () => {
    render(<AddNotificationModal provider={buildProvider({ on_plate_clear_required: false })} onClose={() => undefined} />);

    await screen.findByDisplayValue('My Telegram');
    expect(screen.queryByText(BUTTON_LABEL)).not.toBeInTheDocument();
  });

  it('does not show for other provider types, which still see the required setting', async () => {
    render(
      <AddNotificationModal
        provider={buildProvider({ provider_type: 'ntfy', config: { server: 'https://ntfy.sh', topic: 'x' } })}
        onClose={() => undefined}
      />,
    );

    await screen.findByDisplayValue('My Telegram');
    expect(screen.queryByText(BUTTON_LABEL)).not.toBeInTheDocument();
    expect(screen.getByText(/Only sent while/)).toBeInTheDocument();
  });

  it('submits the chosen value on save', async () => {
    const captured = capturePatch();
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(<AddNotificationModal provider={buildProvider()} onClose={onClose} />);

    await screen.findByText(BUTTON_LABEL);
    await user.click(toggleFor(BUTTON_LABEL));
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured.body).toMatchObject({ provider_type: 'telegram', plate_clear_button: true });
  });

  it('always submits false for a non-Telegram provider', async () => {
    const captured = capturePatch();
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <AddNotificationModal
        provider={buildProvider({
          provider_type: 'ntfy',
          config: { server: 'https://ntfy.sh', topic: 'x' },
          plate_clear_button: true,
        })}
        onClose={onClose}
      />,
    );

    await screen.findByDisplayValue('My Telegram');
    await user.click(screen.getByRole('button', { name: /^save$/i }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(captured.body).toMatchObject({ provider_type: 'ntfy', plate_clear_button: false });
  });
});

describe('NotificationProviderCard — plate-clear button', () => {
  async function expand(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByText('My Telegram');
    await user.click(screen.getByText('Event Settings'));
  }

  it('updates the provider from the expanded card', async () => {
    const captured = capturePatch();
    const user = userEvent.setup();
    render(<NotificationProviderCard provider={buildProvider()} onEdit={vi.fn()} />);

    await expand(user);
    await user.click(toggleFor(BUTTON_LABEL));

    await waitFor(() => expect(captured.body).toEqual({ plate_clear_button: true }));
  });

  it('is not offered with the event off or on another provider type', async () => {
    const user = userEvent.setup();
    const { unmount } = render(
      <NotificationProviderCard provider={buildProvider({ on_plate_clear_required: false })} onEdit={vi.fn()} />,
    );
    await expand(user);
    expect(screen.queryByText(BUTTON_LABEL)).not.toBeInTheDocument();
    unmount();

    render(
      <NotificationProviderCard
        provider={buildProvider({ provider_type: 'ntfy', config: { server: 'https://ntfy.sh', topic: 'x' } })}
        onEdit={vi.fn()}
      />,
    );
    await expand(user);
    expect(screen.queryByText(BUTTON_LABEL)).not.toBeInTheDocument();
  });
});
