/** Single-user browser session: login, CSRF token and declared database identity. */
import {api, ApiError, setGuard, str, fact, type Fact} from './client.ts'

export type Session = {username: string; csrf: string; expires_at: string}
const state: {session: Session | null; identity: [string, number] | null} = {session: null, identity: null}
let epoch=0
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
export function forgetIdentity(): void { epoch++;state.identity = null }
export function forgetSession(): void { forgetIdentity();state.session=null }
export async function login(username: string, password: string): Promise<Session> {
  const token=++epoch
  const value=session(await api('/auth/login', 'POST', {username,password}))
  if(token!==epoch)throw new Error('登录请求已失效，请重新登录')
  state.session=value
  return value
}
export async function stored(): Promise<Session | null> {
  const token=++epoch
  try {
    const value=session(await api('/auth/session'))
    if(token!==epoch)return null
    state.session=value
    return value
  }catch(e){
    if(token!==epoch)return null
    state.session=null
    if(e instanceof ApiError&&e.status===401)return null
    throw e
  }
}
export async function logout(): Promise<void> {
  const pending=api('/auth/logout', 'POST', {})
  forgetSession()
  await pending
}
