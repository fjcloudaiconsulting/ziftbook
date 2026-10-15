import { LegalPage, legalTitle } from "../_ui/legal";

export const generateMetadata = () => legalTitle("privacy");

export default function Page() {
  return <LegalPage doc="privacy" />;
}
