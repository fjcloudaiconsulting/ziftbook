import { LegalPage, legalTitle } from "../_ui/legal";

export const generateMetadata = (props: PageProps<"/[locale]/privacy">) => legalTitle(props, "privacy");

export default function Page(props: PageProps<"/[locale]/privacy">) {
  return <LegalPage {...props} doc="privacy" />;
}
