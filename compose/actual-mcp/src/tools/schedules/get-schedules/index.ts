// ----------------------------
// GET SCHEDULES TOOL
// ----------------------------

import { CallToolResult } from '@modelcontextprotocol/sdk/types.js';
import { z, toJSONSchema } from 'zod';
import { success, errorFromCatch } from '../../../utils/response.js';
import { formatAmount } from '../../../utils.js';
import { getAccounts, getPayees, getSchedules, type Schedule } from '../../../actual-api.js';
import { type ToolInput } from '../../../types.js';

const GetSchedulesArgsSchema = z.object({
  includeCompleted: z.boolean().optional().describe('Also list completed schedules. Defaults to false.'),
});

export const schema = {
  name: 'get-schedules',
  description: 'List scheduled transactions (recurring bills, subscriptions, planned one-offs) with their next date.',
  inputSchema: toJSONSchema(GetSchedulesArgsSchema) as ToolInput,
};

/**
 * Formats a schedule amount, which is a range object when the amount operator is "isbetween".
 */
export function formatScheduleAmount(amount: Schedule['amount']): string {
  if (amount === undefined || amount === null) return 'N/A';
  if (typeof amount === 'number') return formatAmount(amount);
  return `${formatAmount(amount.num1)} – ${formatAmount(amount.num2)}`;
}

/**
 * Describes a schedule's date rule: a fixed date for one-offs, or "every N <frequency>" for recurring ones.
 */
export function formatScheduleFrequency(date: Schedule['date']): string {
  if (!date) return 'N/A';
  if (typeof date === 'string') return `once (${date})`;
  if (!date.interval || date.interval <= 1) return date.frequency;
  const unit = { daily: 'days', weekly: 'weeks', monthly: 'months', yearly: 'years' }[date.frequency];
  return `every ${date.interval} ${unit}`;
}

export async function handler(args: z.infer<typeof GetSchedulesArgsSchema> = {}): Promise<CallToolResult> {
  try {
    const { includeCompleted = false } = GetSchedulesArgsSchema.parse(args ?? {});
    const [schedules, accounts, payees] = await Promise.all([getSchedules(), getAccounts(), getPayees()]);

    const accountNames = new Map(accounts.map((a) => [a.id, a.name]));
    const payeeNames = new Map(payees.map((p) => [p.id, p.name]));
    const listed = schedules
      .filter((s) => includeCompleted || !s.completed)
      .sort((a, b) => (a.next_date ?? '9999').localeCompare(b.next_date ?? '9999'));

    if (listed.length === 0) {
      return success('No scheduled transactions found.');
    }

    const rows = listed.map((s) => {
      const payee = s.payee ? (payeeNames.get(s.payee) ?? s.payee) : 'N/A';
      const account = s.account ? (accountNames.get(s.account) ?? s.account) : 'N/A';
      const status = s.completed ? 'Completed' : 'Active';
      return `| ${s.name || '(unnamed)'} | ${payee} | ${account} | ${formatScheduleAmount(s.amount)} | ${formatScheduleFrequency(s.date)} | ${s.next_date ?? 'N/A'} | ${status} | ${s.id} |`;
    });

    const markdown = [
      '# Scheduled Transactions',
      '',
      `Found ${listed.length} schedule(s)`,
      '',
      '| Name | Payee | Account | Amount | Frequency | Next Date | Status | ID |',
      '|------|-------|---------|--------|-----------|-----------|--------|----|',
      ...rows,
    ].join('\n');
    return success(markdown);
  } catch (err) {
    return errorFromCatch(err);
  }
}
