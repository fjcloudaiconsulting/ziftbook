import { createNavigation } from "next-intl/navigation";

import { routing } from "./routing";

// Locale-aware Link and router: paths are written without the locale ("/sign-in").
export const { Link, usePathname, useRouter } = createNavigation(routing);
