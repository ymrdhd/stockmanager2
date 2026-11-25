import type {Fact} from './client.ts'

export type AccountTicket = {readonly account: string; readonly generation: number}
/** A → B → A is three different scopes, even when the IDs compare equal. */
export class AccountScope {
  private account = ''
  private generation = 0
  reset(account: string, clear: () => void): void {
    this.account = account
    this.generation++
    clear()
  }
  capture(): AccountTicket { return {account:this.account,generation:this.generation} }
  current(ticket: AccountTicket): boolean {
    return ticket.account === this.account && ticket.generation === this.generation
  }
  /** Check scope before inspecting response contents. Never publish a mismatched response. */
  accepts(ticket: AccountTicket, ...responses: Fact[]): boolean {
    if (!this.current(ticket)) return false
    if (responses.some(r => r.account_id !== ticket.account)) throw new Error('服务器返回的证券账户与查询不一致，已拒绝展示')
    return true
  }
}
