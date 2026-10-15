import { LegalPage, legalTitle } from "../_ui/legal";

export const generateMetadata = (props: PageProps<"/[locale]/terms">) => legalTitle(props, "terms");

export default function Page(props: PageProps<"/[locale]/terms">) {
  return <LegalPage {...props} doc="terms" />;
}
