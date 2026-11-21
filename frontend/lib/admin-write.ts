// The phases of an admin write that may need a fresh second factor, free of React so node --test can run them
// (ZIF-73 suspension, ZIF-78 billing). The reason form sends the write; a stale factor (403) opens the code form;
// a passed code sends the write again.
import { factorState, writeStep } from "./admin.ts";

export type Recheck = { phase: "reason" | "code"; kind: "app" | "recovery"; blocked: false | "locked" | "unavailable" };
export const initialRecheck: Recheck = { phase: "reason", kind: "app", blocked: false };

export type Event =
  | { type: "write"; status: number; code?: string } // the answer to the write
  | { type: "code"; status: number; code?: string } // the answer to a second-factor code
  | { type: "switch"; kind: "app" | "recovery" }
  | { type: "reset" };

/** What the caller does next: a write's step (writeStep) for "write"; for "code": send the write again, say the
 * code is wrong, say only a recovery code can work now, say too many tries, or show the generic failure. */
export type Then = ReturnType<typeof writeStep> | "resend" | "wrong" | "blocked" | "tooMany";

export function recheck(state: Recheck, event: Event): { state: Recheck; then?: Then } {
  switch (event.type) {
    case "write": {
      const step = writeStep(event.status, event.code);
      return { state: step === "recheck" ? { ...state, phase: "code" } : state, then: step };
    }
    case "code": {
      if (event.status === 200) return { state: { ...state, phase: "reason" }, then: "resend" };
      const answer = factorState(event.status, event.code);
      if (answer === "wrong") return { state, then: "wrong" };
      // A dead or replaced session, or a conflict: back through sign-in, which resumes at the right step.
      if (answer === "expired" || answer === "conflict") return { state, then: "signIn" };
      if (answer === "locked" || answer === "unavailable") return { state: { ...state, blocked: answer, kind: "recovery" }, then: "blocked" };
      return { state, then: answer === "tooMany" ? "tooMany" : "other" };
    }
    case "switch":
      // With the app unusable, there is no way back to it.
      return { state: state.blocked && event.kind === "app" ? state : { ...state, kind: event.kind } };
    case "reset":
      return { state: initialRecheck };
  }
}
