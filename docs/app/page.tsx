import DocsPage from "./docs-page";
import { getPage } from "./content";

export default function Home() {
  return <DocsPage page={getPage("overview")!} />;
}
