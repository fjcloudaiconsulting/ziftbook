import { defineRouting } from "next-intl/routing";

export const routing = defineRouting({
  locales: ["en", "nl", "pt"],
  defaultLocale: "en",
  // The URL carries the language; signed-in users will carry users.locale. No cookie.
  localeCookie: false,
});
