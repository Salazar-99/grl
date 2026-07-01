"use client";

import { useEffect, useState } from "react";

type InPageNavProps = {
  title: string;
  items: {
    id: string;
    label: string;
  }[];
};

export default function InPageNav({ title, items }: InPageNavProps) {
  const [activeId, setActiveId] = useState(items[0]?.id ?? "");

  useEffect(() => {
    const sections = items
      .map((item) => document.getElementById(item.id))
      .filter((section): section is HTMLElement => section !== null);

    if (sections.length === 0) {
      return;
    }

    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);

        if (visible[0]?.target.id) {
          setActiveId(visible[0].target.id);
        }
      },
      {
        rootMargin: "-18% 0px -65% 0px",
        threshold: [0, 1],
      },
    );

    sections.forEach((section) => observer.observe(section));

    return () => observer.disconnect();
  }, [items]);

  return (
    <aside className="toc" aria-label="On this page">
      <nav className="toc-card">
        <a className="toc-title" href="#top" onClick={() => setActiveId("")}>
          {title}
        </a>
        {items.map((item) => (
          <a
            aria-current={activeId === item.id ? "true" : undefined}
            className={activeId === item.id ? "active" : ""}
            href={`#${item.id}`}
            key={item.id}
            onClick={() => setActiveId(item.id)}
          >
            {item.label}
          </a>
        ))}
      </nav>
    </aside>
  );
}
