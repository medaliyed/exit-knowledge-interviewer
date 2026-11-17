import { useId, useRef, useState, type FormEvent, type ReactNode, type Ref } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ApiClient, ApiError } from './api';
import type { Identity } from './types';
import { Button, Modal, Notice } from './ui';

export type Message = { text: string; error: boolean };
export const INVALID_LINK =
  'This link is no longer valid. Ask the person who invited you for a new one.';

export function authError(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 429)
      return `Too many attempts. Try again in ${Math.max(1, Math.ceil((error.retryAfter ?? 60) / 60))} minutes.`;
    if (error.status === 503)
      return 'Sign-in is not configured on this server. Ask the person who prepared your handover.';
    if (error.status === 401) return 'Email or password is incorrect.';
    if (error.code === 'invalid_link') return INVALID_LINK;
    if (error.code === 'weak_password' || error.code === 'current_password_incorrect')
      return error.message;
  }
  return 'The workspace could not be reached. Try again.';
}

function Welcome({ children }: { children: ReactNode }) {
  return (
    <>
      <header className="site-header">
        <Link to="/" className="wordmark">
          eki
        </Link>
        <span className="brand-subtitle">Exit-Knowledge Interviewer</span>
      </header>
      <main id="main-content" className="welcome">
        <section className="welcome-story">
          <p className="eyebrow">Keep the knowledge. Honour the source.</p>
          <h1>
            A thoughtful handover.
            <br />A lasting reference.
          </h1>
          <ol className="welcome-steps">
            {[
              [
                'Make a plan',
                'eki reads the selected tickets, code history and emails, and drafts the topics only you know. You decide what stays.',
              ],
              [
                'Have the conversation',
                'One question at a time, over as many sittings as you like. Submitted answers are saved.',
              ],
              [
                'Make it yours',
                'Read each runbook entry, correct it, and approve it. Nothing is published without you.',
              ],
            ].map(([title, detail], index) => (
              <li key={title}>
                <span className="step-number">{index + 1}</span>
                <div>
                  <h2>{title}</h2>
                  <p>{detail}</p>
                </div>
              </li>
            ))}
          </ol>
        </section>
        {children}
      </main>
      <footer className="site-footer">A thoughtful handover. A lasting reference.</footer>
    </>
  );
}

function PasswordField({
  id,
  label,
  value,
  onChange,
  disabled,
  autoComplete,
  invalid = false,
  fieldRef,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  disabled: boolean;
  autoComplete: 'current-password' | 'new-password';
  invalid?: boolean;
  fieldRef?: Ref<HTMLInputElement>;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <div className="form-field">
      <label htmlFor={id}>{label}</label>
      <div className="password-field">
        <input
          ref={fieldRef}
          id={id}
          name={id}
          type={visible ? 'text' : 'password'}
          required
          autoComplete={autoComplete}
          spellCheck={false}
          value={value}
          onChange={event => onChange(event.target.value)}
          disabled={disabled}
          aria-invalid={invalid}
        />
        <Button
          variant="quiet"
          aria-controls={id}
          aria-label={`${visible ? 'Hide' : 'Show'} ${label.toLowerCase()}`}
          aria-pressed={visible}
          disabled={disabled}
          onClick={() => setVisible(!visible)}
        >
          {visible ? 'Hide' : 'Show'}
        </Button>
      </div>
    </div>
  );
}

export function SignIn({
  onSignIn,
  message,
}: {
  onSignIn: (email: string, password: string) => Promise<void>;
  message: Message;
}) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const passwordInput = useRef<HTMLInputElement>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      await onSignIn(email.trim(), password);
      setPassword('');
    } catch (err) {
      setError(authError(err));
      setPassword('');
    } finally {
      setBusy(false);
      requestAnimationFrame(() => passwordInput.current?.focus());
    }
  }
  return (
    <Welcome>
      <section className="sheet login-card" aria-labelledby="signin-title">
        <p className="eyebrow">Your knowledge, in good hands</p>
        <h2 id="signin-title">Sign in</h2>
        <form onSubmit={submit} className="stack">
          <div className="form-field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              name="email"
              type="email"
              required
              autoComplete="username"
              maxLength={254}
              value={email}
              onChange={event => setEmail(event.target.value)}
              disabled={busy}
              aria-invalid={!!error}
            />
          </div>
          <PasswordField
            id="password"
            label="Password"
            value={password}
            onChange={setPassword}
            disabled={busy}
            autoComplete="current-password"
            invalid={!!error}
            fieldRef={passwordInput}
          />
          {(error || message.text) && (
            <Notice error={!!error || message.error}>{error || message.text}</Notice>
          )}
          <Button variant="primary" type="submit" disabled={busy}>
            {busy ? 'Signing in…' : 'Sign in'}
            <span aria-hidden="true">↗</span>
          </Button>
          <p className="field-help">
            Forgot your password? Ask the person who invited you for a reset link.
          </p>
        </form>
        <p className="login-footnote">A saved conversation is ready whenever you are.</p>
      </section>
    </Welcome>
  );
}

function NewPasswords({
  password,
  confirm,
  setPassword,
  setConfirm,
  busy,
  error,
}: {
  password: string;
  confirm: string;
  setPassword: (value: string) => void;
  setConfirm: (value: string) => void;
  busy: boolean;
  error: boolean;
}) {
  const id = useId();
  return (
    <>
      <PasswordField
        id={`${id}-new`}
        label="New password"
        value={password}
        onChange={setPassword}
        disabled={busy}
        autoComplete="new-password"
        invalid={error}
      />
      <PasswordField
        id={`${id}-confirm`}
        label="Confirm password"
        value={confirm}
        onChange={setConfirm}
        disabled={busy}
        autoComplete="new-password"
        invalid={error}
      />
      <ul className="password-checklist" aria-label="Password requirements">
        <li>
          {[...password].length >= 12 ? '✓' : '○'} 12 or more characters{' '}
          <span className="sr-only">{[...password].length >= 12 ? 'met' : 'not met'}</span>
        </li>
        <li>
          ○ Not a common password <span className="field-help">(checked when you submit)</span>
        </li>
      </ul>
    </>
  );
}
function passwordError(password: string, confirm: string) {
  if (password !== confirm) return 'The passwords do not match.';
  if ([...password].length < 12) return 'Use 12 or more characters for your password.';
  if ([...password].length > 128) return 'Use no more than 128 characters for your password.';
  return '';
}

export function SetPassword({
  token,
  onComplete,
  onReturn,
}: {
  token: string;
  onReturn: () => void;
  onComplete: (token: string, password: string) => Promise<void>;
}) {
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [invalidLink, setInvalidLink] = useState(!token);
  const navigate = useNavigate();
  async function submit(event: FormEvent) {
    event.preventDefault();
    const validation = passwordError(password, confirm);
    if (validation) {
      setError(validation);
      return;
    }
    setBusy(true);
    setError('');
    try {
      await onComplete(token, password);
      setPassword('');
      setConfirm('');
      navigate('/workspace', { replace: true });
    } catch (err) {
      if (err instanceof ApiError && err.code === 'invalid_link') setInvalidLink(true);
      setError(authError(err));
      setPassword('');
      setConfirm('');
    } finally {
      setBusy(false);
    }
  }
  return (
    <Welcome>
      <section className="sheet login-card" aria-labelledby="setup-title">
        <p className="eyebrow">Your knowledge, in good hands</p>
        <h2 id="setup-title">Set your password</h2>
        {invalidLink ? (
          <div className="stack">
            <Notice error>{INVALID_LINK}</Notice>
            <Link to="/" onClick={onReturn}>
              Return to sign in
            </Link>
          </div>
        ) : (
          <form onSubmit={submit} className="stack">
            <NewPasswords
              {...{ password, confirm, setPassword, setConfirm, busy }}
              error={!!error}
            />
            {error && <Notice error>{error}</Notice>}
            <Button variant="primary" type="submit" disabled={busy}>
              {busy ? 'Setting password…' : 'Set password and sign in'}
            </Button>
          </form>
        )}
      </section>
    </Welcome>
  );
}

export function AccountMenu({
  identity,
  signOut,
  changePassword,
}: {
  identity: Identity;
  signOut: () => void;
  changePassword: () => void;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  const close = () => {
    if (menu.current) menu.current.open = false;
  };
  return (
    <details
      className="account-menu"
      ref={menu}
      onKeyDown={event => {
        if (event.key === 'Escape') {
          close();
          menu.current?.querySelector('summary')?.focus();
        }
      }}
    >
      <summary className="identity">
        Signed in as <strong>{identity.name}</strong>
        <span aria-hidden="true"> ▾</span>
      </summary>
      <div className="account-actions">
        <Button
          variant="quiet"
          onClick={() => {
            close();
            changePassword();
          }}
        >
          Change password
        </Button>
        <Button
          variant="quiet"
          onClick={() => {
            close();
            signOut();
          }}
        >
          Sign out
        </Button>
      </div>
    </details>
  );
}

export function ChangePassword({
  api,
  onClose,
  onSuccess,
}: {
  api: ApiClient;
  onClose: () => void;
  onSuccess: () => void;
}) {
  const [current, setCurrent] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent) {
    event.preventDefault();
    const validation = passwordError(password, confirm);
    if (validation) {
      setError(validation);
      return;
    }
    setBusy(true);
    setError('');
    try {
      await api.request<void>('/auth/password', 'POST', {
        current_password: current,
        new_password: password,
      });
      setCurrent('');
      setPassword('');
      setConfirm('');
      onSuccess();
    } catch (err) {
      setError(authError(err));
      setCurrent('');
      setPassword('');
      setConfirm('');
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title="Change password"
      open
      onClose={() => {
        if (!busy) onClose();
      }}
    >
      <form className="stack" onSubmit={submit}>
        <PasswordField
          id="current-password"
          label="Current password"
          value={current}
          onChange={setCurrent}
          disabled={busy}
          autoComplete="current-password"
          invalid={!!error}
        />
        <NewPasswords {...{ password, confirm, setPassword, setConfirm, busy }} error={!!error} />
        {error && <Notice error>{error}</Notice>}
        <div className="toolbar">
          <Button variant="primary" type="submit" disabled={busy}>
            {busy ? 'Changing password…' : 'Change password'}
          </Button>
          <Button disabled={busy} onClick={onClose}>
            Cancel
          </Button>
        </div>
      </form>
    </Modal>
  );
}
