import type { routing } from "./i18n/routing";
import type messages from "./messages/en.json";

// Makes locales and message keys type-checked: a missing or misspelt key fails `tsc`.
declare module "next-intl" {
  interface AppConfig {
    Locale: (typeof routing.locales)[number];
    Messages: typeof messages;
  }
}
