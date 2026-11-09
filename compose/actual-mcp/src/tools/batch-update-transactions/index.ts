import { CallToolResult } from '@modelcontextprotocol/sdk/types.js';
import { toJSONSchema } from 'zod';
import { success, errorFromCatch } from '../../utils/response.js';
import { BatchUpdateTransactionsArgsSchema, type BatchUpdateTransactionsArgs, ToolInput } from '../../types.js';
import { applyTransactionUpdate } from '../update-transaction/apply-update.js';

export const schema = {
  name: 'batch-update-transactions',
  description:
    'Update up to 100 transactions in one call, with the same fields as update-transaction. Useful for bulk ' +
    'categorization or turning several transactions into transfers. Each update succeeds or fails on its own.',
  inputSchema: toJSONSchema(BatchUpdateTransactionsArgsSchema) as ToolInput,
};

export async function handler(args: BatchUpdateTransactionsArgs): Promise<CallToolResult> {
  try {
    const { updates } = BatchUpdateTransactionsArgsSchema.parse(args);

    const succeeded: string[] = [];
    const failed: string[] = [];
    // Reason: sequential on purpose — writes go through one shared connection queue anyway,
    // and a stable order keeps transfer counterparts predictable.
    for (const update of updates) {
      try {
        const result = await applyTransactionUpdate(update);
        if (result.ok) {
          succeeded.push(result.id);
        } else {
          failed.push(`- ${update.id}: ${result.error}`);
        }
      } catch (err) {
        failed.push(`- ${update.id}: ${err instanceof Error ? err.message : String(err)}`);
      }
    }

    let message = `Batch update complete: ${succeeded.length}/${updates.length} succeeded`;
    if (failed.length > 0) {
      message += `\n\nFailed updates:\n${failed.join('\n')}`;
    }
    if (succeeded.length > 0) {
      message += `\n\nSuccessfully updated: ${succeeded.join(', ')}`;
    }
    return success(message);
  } catch (err) {
    return errorFromCatch(err);
  }
}
