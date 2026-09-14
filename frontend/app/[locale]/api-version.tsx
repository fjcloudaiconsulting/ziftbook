"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { healthHealthz } from "@/api-client";

type State = { kind: "checking" } | { kind: "ok"; version: string } | { kind: "unavailable" };

// Walking skeleton: proves the browser reaches FastAPI through the same-origin /api proxy.
export function ApiVersion() {
  const t = useTranslations("HomePage");
  const [state, setState] = useState<State>({ kind: "checking" });

  useEffect(() => {
    healthHealthz()
      .then(({ data }) => setState(data ? { kind: "ok", version: data.version } : { kind: "unavailable" }))
      .catch(() => setState({ kind: "unavailable" }));
  }, []);

  return (
    <p role="status">
      {state.kind === "ok" && t("apiVersion", { version: state.version })}
      {state.kind === "checking" && t("apiChecking")}
      {state.kind === "unavailable" && t("apiUnavailable")}
    </p>
  );
}
