import fs from "node:fs";
import path from "node:path";
import matter from "gray-matter";

export type DocsPage = {
  slug: string;
  navTitle: string;
  title: string;
  eyebrow: string;
  intro: string;
  toc: string[];
  content: string;
};

export const sidebarSections = [
  {
    title: "Introduction",
    links: [
      { title: "Overview", slug: "overview" },
      { title: "System architecture", slug: "system-architecture" },
      { title: "Execution model", slug: "execution-model" },
    ],
  },
  {
    title: "Training",
    links: [
      { title: "GRPO pipeline", slug: "grpo-pipeline" },
      { title: "Rollout workers", slug: "rollout-workers" },
      { title: "Weight synchronization", slug: "weight-synchronization" },
      { title: "Telemetry", slug: "telemetry" },
    ],
  },
  {
    title: "Infrastructure",
    links: [
      { title: "EKS cluster", slug: "eks-cluster" },
      { title: "Ray node groups", slug: "ray-node-groups" },
      { title: "Environment manager", slug: "environment-manager" },
      { title: "Observability", slug: "observability" },
    ],
  },
  {
    title: "Reference",
    links: [
      { title: "Configuration", slug: "configuration" },
      { title: "Launcher CLI", slug: "launcher-cli" },
      { title: "Environment API", slug: "environment-api" },
      { title: "Runbooks", slug: "runbooks" },
    ],
  },
];

const contentDirectory = path.join(process.cwd(), "content");

function getSidebarLink(slug: string) {
  return sidebarSections.flatMap((section) => section.links).find((link) => link.slug === slug);
}

function headingToToc(content: string) {
  return content
    .split("\n")
    .filter((line) => line.startsWith("## "))
    .map((line) => line.replace(/^##\s+/, "").trim());
}

export function getPageSlugs() {
  return sidebarSections.flatMap((section) => section.links.map((link) => link.slug));
}

export function getPage(slug: string): DocsPage | undefined {
  const sidebarLink = getSidebarLink(slug);

  if (!sidebarLink) {
    return undefined;
  }

  const filePath = path.join(contentDirectory, `${slug}.mdx`);

  if (!fs.existsSync(filePath)) {
    return {
      slug,
      navTitle: sidebarLink.title,
      title: sidebarLink.title,
      eyebrow: "Docs",
      intro: "This page is ready for MDX content.",
      toc: ["Content"],
      content: "## Content\n\nCreate `docs/content/" + slug + ".mdx` to fill in this page.",
    };
  }

  const source = fs.readFileSync(filePath, "utf8");
  const { content, data } = matter(source);

  return {
    slug,
    navTitle: data.navTitle ?? data.title ?? slug,
    title: data.title ?? slug,
    eyebrow: data.eyebrow ?? "Docs",
    intro: data.intro ?? "",
    toc: headingToToc(content),
    content,
  };
}
