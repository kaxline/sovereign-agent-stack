import { updateTransaction } from '../../actual-api.js';
import { resolveTransferPayee } from '../../core/data/resolve-transfer-payee.js';
import { UpdateTransactionArgsSchema } from '../../types.js';

export type ApplyUpdateResult = { ok: true; id: string; updatedFields: string[] } | { ok: false; error: string };

/**
 * Validates one update-transaction payload, resolves transfer payees and writes it.
 * Shared by update-transaction and batch-update-transactions so both behave identically.
 *
 * @param args - Raw tool arguments for a single transaction update
 * @returns The updated field names, or a user-facing error message
 */
export async function applyTransactionUpdate(args: unknown): Promise<ApplyUpdateResult> {
  // Reason: payee_name is only resolved on the create/import path in @actual-app/api.
  // On update it reaches an unguarded schema-conform step that throws asynchronously
  // after the call already appears to succeed, crashing the whole server process
  // (upstream https://github.com/s-stefanov/actual-mcp/issues/150). Reject it here
  // with a clear alternative rather than silently dropping it or letting it crash.
  if (args && typeof args === 'object' && 'payee_name' in args) {
    return {
      ok: false,
      error:
        'payee_name is not supported on update-transaction (it can crash the server). ' +
        'Use create-payee to get or create a payee, then pass its id via the payee field instead.',
    };
  }

  const {
    id: transactionId,
    transfer_account_id: transferAccountId,
    ...updateData
  } = UpdateTransactionArgsSchema.parse(args);

  // Reason: Turning the whole transaction into a transfer means assigning the destination
  // account's transfer payee; Actual then creates the counterpart transaction itself.
  if (transferAccountId) {
    const transferPayeeId = await resolveTransferPayee(transferAccountId);
    if (!transferPayeeId) {
      return {
        ok: false,
        error: `No transfer payee found for account ${transferAccountId}. Ensure the destination account exists.`,
      };
    }
    updateData.payee = transferPayeeId;
  }

  // Reason: A subtransaction can itself be a transfer leg (e.g. turning one line of an
  // existing split into a transfer to savings). Resolve each subtransaction's
  // transfer_account_id into the corresponding transfer payee before sending to the API.
  if (updateData.subtransactions) {
    for (const sub of updateData.subtransactions) {
      if (sub.transfer_account_id) {
        const subTransferPayeeId = await resolveTransferPayee(sub.transfer_account_id);
        if (!subTransferPayeeId) {
          return {
            ok: false,
            error: `No transfer payee found for account ${sub.transfer_account_id}. Ensure the destination account exists.`,
          };
        }
        sub.payee = subTransferPayeeId;
      }
      delete sub.transfer_account_id;
    }
  }

  // Filter out undefined values to only send fields that were explicitly provided
  const filteredUpdateData = Object.fromEntries(Object.entries(updateData).filter(([, value]) => value !== undefined));

  if (Object.keys(filteredUpdateData).length === 0) {
    return { ok: false, error: 'No fields provided to update. Please specify at least one field to modify.' };
  }

  await updateTransaction(transactionId, filteredUpdateData);
  return { ok: true, id: transactionId, updatedFields: Object.keys(filteredUpdateData) };
}
