import { notFound } from "next/navigation";
import DocsPage from "../docs-page";
import { getPage, getPageSlugs } from "../content";

type PageProps = {
  params: Promise<{
    slug: string;
  }>;
};

export function generateStaticParams() {
  return getPageSlugs().map((slug) => ({ slug }));
}

export async function generateMetadata({ params }: PageProps) {
  const { slug } = await params;
  const page = getPage(slug);

  if (!page) {
    return {};
  }

  return {
    title: `${page.title} | GRL Docs`,
    description: page.intro,
  };
}

export default async function Page({ params }: PageProps) {
  const { slug } = await params;
  const page = getPage(slug);

  if (!page) {
    notFound();
  }

  return <DocsPage page={page} />;
}
