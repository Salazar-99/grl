import Link from "next/link";
import { MDXRemote } from "next-mdx-remote/rsc";
import rehypePrettyCode from "rehype-pretty-code";
import rehypeSlug from "rehype-slug";
import remarkGfm from "remark-gfm";
import { DocsPage as DocsPageContent, sidebarSections } from "./content";
import InPageNav from "./in-page-nav";

type DocsPageProps = {
  page: DocsPageContent;
};

function tocId(label: string) {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

export default async function DocsPage({ page }: DocsPageProps) {
  const tocItems = page.toc.map((item) => ({
    id: tocId(item),
    label: item,
  }));

  return (
    <main className="docs-shell">
      <header className="topbar">
        <Link className="brand" href="/overview">
          <span className="brand-pixel">grl</span>
          <span className="brand-docs">Docs</span>
        </Link>
        <a className="repo-star" href="https://github.com/Salazar-99/grl" rel="noreferrer" target="_blank">
          <svg aria-hidden="true" viewBox="0 0 16 16">
            <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82A7.57 7.57 0 0 1 8 3.86c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
          </svg>
          <span>Star the Repo</span>
        </a>
      </header>

      <div className="docs-layout">
        <aside className="sidebar" aria-label="Documentation sections">
          {sidebarSections.map((section) => (
            <section key={section.title}>
              <h2>{section.title}</h2>
              <ul>
                {section.links.map((link) => (
                  <li key={link.slug}>
                    <Link className={link.slug === page.slug ? "active" : ""} href={`/${link.slug}`}>
                      {link.title}
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          ))}
        </aside>

        <article className="content">
          <div className="notice">Beta: This documentation site is ready for publishing as a private image.</div>

          <div className="content-header" id="top">
            <p className="eyebrow">{page.eyebrow}</p>
            <h1>{page.title}</h1>
            <p>{page.intro}</p>
          </div>

          <div className="mdx-content">
            <MDXRemote
              options={{
                mdxOptions: {
                  remarkPlugins: [remarkGfm],
                  rehypePlugins: [
                    rehypeSlug,
                    [
                      rehypePrettyCode,
                      {
                        theme: "github-dark",
                      },
                    ],
                  ],
                },
              }}
              source={page.content}
            />
          </div>
        </article>

        <InPageNav items={tocItems} title={page.navTitle} />
      </div>
    </main>
  );
}
