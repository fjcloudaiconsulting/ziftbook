import { CspGuard } from "../_ui/csp-guard";

// ZIF-146: a personal page (lib/personal-pages.ts) reloads if a client-side navigation brought it into a document
// whose policy only reports.
export default function PersonalLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <CspGuard />
      {children}
    </>
  );
}
