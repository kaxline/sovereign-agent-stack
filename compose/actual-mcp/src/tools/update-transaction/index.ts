import { CallToolResult } from '@modelcontextprotocol/sdk/types.js';
import { toJSONSchema } from 'zod';
import { error, success, errorFromCatch } from '../../utils/response.js';
import { UpdateTransactionArgsSchema, type UpdateTransactionArgs, ToolInput } from '../../types.js';
import { applyTransactionUpdate } from './apply-update.js';

export const schema = {
  name: 'update-transaction',
  description:
    'Update an existing transaction. Can modify date, amount, payee, category, notes, cleared status, and ' +
    'subtransactions, and turn the transaction or one of its subtransactions into a transfer via transfer_account_id.',
  inputSchema: toJSONSchema(UpdateTransactionArgsSchema) as ToolInput,
};

export async function handler(args: UpdateTransactionArgs): Promise<CallToolResult> {
  try {
    const result = await applyTransactionUpdate(args);
    if (!result.ok) {
      return error(result.error);
    }
    return success(`Successfully updated transaction ${result.id}. Updated fields: ${result.updatedFields.join(', ')}`);
  } catch (err) {
    return errorFromCatch(err);
  }
}
