'use client';

import Image from 'next/image';
import { useEffect, useState } from 'react';
import { ArrowRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { apiFetch } from '@/lib/api';
import { message, type User } from '@/lib/relay';

export function Auth({ onLogin, initialError }: { onLogin: (user: User) => void; initialError: string }) {
  const [register, setRegister] = useState(false);
  const [registrationEnabled, setRegistrationEnabled] = useState<boolean | null>(null);
  const [policyFailed, setPolicyFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    apiFetch<{ registration_enabled: boolean }>('/api/auth/policy', { signal: controller.signal })
      .then(policy => setRegistrationEnabled(policy.registration_enabled))
      .catch(() => { if (!controller.signal.aborted) setPolicyFailed(true); });
    return () => controller.abort();
  }, []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  return <main className="auth-shell">
    <section className="auth-story"><div className="brand"><Image src="/icon.svg" width={30} height={30} alt="" /><span>RelayOps</span></div><div><span className="eyebrow">EVERY EVENT. ACCOUNTED FOR.</span><h1>A reliable route<br />for your webhooks.</h1><p>Receive events, follow every delivery, and give failed requests another chance.</p><div className="auth-flow"><span>Receive</span><ArrowRight /><span>Persist</span><ArrowRight /><span>Deliver</span></div></div><small>Webhook reliability, without the guesswork.</small></section>
    <section className="auth-form"><Card><CardHeader><span className="eyebrow">YOUR WORKSPACE</span><CardTitle><h2>{register ? 'Create your account' : 'Welcome back'}</h2></CardTitle><p>{register ? 'Start receiving and delivering events.' : 'Sign in to see your event traffic.'}</p></CardHeader><CardContent>
      <form onSubmit={async event => {
        event.preventDefault(); setBusy(true); setError('');
        try {
          const payload = JSON.stringify({ email, password });
          if (register) await apiFetch('/api/auth/register', { method: 'POST', body: payload });
          const user = await apiFetch<User>('/api/auth/login', { method: 'POST', body: payload });
          setPassword(''); onLogin(user);
        } catch (error) { setError(message(error)); } finally { setBusy(false); }
      }}>
        <label htmlFor="email">Email address</label><Input id="email" type="email" autoComplete="email" required maxLength={254} value={email} onChange={e => setEmail(e.target.value)} placeholder="you@company.com" />
        <label htmlFor="password">Password</label><Input id="password" type="password" autoComplete={register ? 'new-password' : 'current-password'} required minLength={register ? 8 : 1} maxLength={128} value={password} onChange={e => setPassword(e.target.value)} />
        {error && <p className="error" role="alert">{error}</p>}
        <Button type="submit" disabled={busy} className="auth-submit">{busy ? 'Please wait…' : register ? 'Create account' : 'Sign in'}<ArrowRight size={16} /></Button>
      </form>
      {registrationEnabled === true ? <p className="auth-switch">{register ? 'Already have an account?' : 'New to RelayOps?'} <Button variant="link" disabled={busy} onClick={() => { setRegister(!register); setError(''); }}>{register ? 'Sign in' : 'Create an account'}</Button></p> : <p className="hint" role="status">{registrationEnabled === false ? 'Registration is disabled. Existing users can sign in. Contact the instance administrator for access.' : policyFailed ? 'Registration availability could not be checked. Sign in or reload to try again.' : 'Checking registration availability…'}</p>}
    </CardContent></Card></section>
  </main>;
}
