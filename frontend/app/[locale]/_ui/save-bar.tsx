"use client";

import { FooterPortal } from "./console";
import styles from "./console.module.css";
import { Submit } from "./parts";
import uiStyles from "./ui.module.css";

/**
 * The sticky bottom bar of a form (week editor, settings): a hint, an optional Undo, and Save,
 * which submits the form `formId` from outside it. Phone: portaled into the shell's own bottom bar
 * (FooterPortal); desktop: the duplicate below, sticky on its own (each hidden where the other
 * applies). `idle` renders the disabled Save of a form with nothing to save.
 */
export function SaveBar({
  formId,
  hint,
  busy,
  idle,
  onUndo,
  saveLabel,
  savingLabel,
  undoLabel,
}: {
  formId: string;
  hint: string;
  busy: boolean;
  idle?: boolean;
  onUndo?: () => void;
  saveLabel: string;
  savingLabel: string;
  undoLabel: string;
}) {
  const content = (
    <>
      <p className={uiStyles.hint} role={busy ? "status" : undefined}>
        {hint}
      </p>
      {onUndo && (
        <button className={uiStyles.textButton} type="button" onClick={onUndo}>
          {undoLabel}
        </button>
      )}
      {idle ? (
        <button
          className={`${uiStyles.button} ${uiStyles.primary} ${styles.saveButton} ${styles.saveIdle}`}
          type="submit"
          form={formId}
          aria-disabled="true"
        >
          {saveLabel}
        </button>
      ) : (
        <div className={styles.saveButton}>
          <Submit busy={busy} busyLabel={savingLabel} form={formId}>
            {saveLabel}
          </Submit>
        </div>
      )}
    </>
  );
  return (
    <>
      <FooterPortal>
        <div className={styles.savebar}>{content}</div>
      </FooterPortal>
      <div className={`${styles.savebar} ${styles.savebarDesktop}`}>{content}</div>
    </>
  );
}
