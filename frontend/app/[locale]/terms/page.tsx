import { LegalPage, legalTitle } from "../_ui/legal";

export const generateMetadata = () => legalTitle("terms");

export default function Page() {
  return <LegalPage doc="terms" />;
}
