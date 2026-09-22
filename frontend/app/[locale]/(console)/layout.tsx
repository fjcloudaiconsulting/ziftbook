import type { ReactNode } from "react";

import { Shell } from "../_ui/console";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  return <Shell>{children}</Shell>;
}
