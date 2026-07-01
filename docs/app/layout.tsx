import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "GRL Docs",
  description: "Documentation for GRL distributed RL training infrastructure.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
