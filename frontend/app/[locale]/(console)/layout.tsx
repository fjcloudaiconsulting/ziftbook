import type { ReactNode } from "react";

import { appVersion } from "@/lib/version";

import { Shell } from "../_ui/console";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return <Shell version={appVersion()}>{children}</Shell>;
}
