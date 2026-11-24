/** Single-user browser session: login, CSRF token and declared database identity. */
import {api, setGuard, str, fact, type Fact} from './client.ts'

export type Session = {username: string; csrf: string; expires_at: string}
const state: {session: Session | null; identity: [string, number] | null} = {session: null, identity: null}
setGuard(() => ({...(state.session ? {csrf: state.session.csrf} : {}), ...(state.identity ? {identity: state.identity} : {})}))

function session(value: Fact): Session {
  const username = str(value.username), csrf = str(value.csrf), expires = str(value.expires_at)
  if (!username || !csrf) throw new Error('登录响应缺少会话令牌')
  return {username, csrf, expires_at: expires}
}
export function current(): Session | null { return state.session }
/** Remember the verified database identity so every later write declares it. */
export function declare(status: Fact): void {
  const identity = status.identity
  if (!identity || typeof identity !== 'object') { state.identity = null; return }
  const value = fact(identity)
  state.identity = [str(value.database_uuid), Number(value.generation)]
}
export function forgetIdentity(): void { state.identity = null }
export async function login(username: string, password: string): Promise<Session> {
  state.session = session(await api('/auth/login', 'POST', {username, password}))
  return state.session
}
export async function stored(): Promise<Session | null> {
  try { state.session = session(await api('/auth/session')); return state.session }
  catch { state.session = null; return null }
}
export async function logout(): Promise<void> {
  try { await api('/auth/logout', 'POST', {}) } finally { state.session = null }
}
