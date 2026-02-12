import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Soundscape Monitor",
  description: "Real-time acoustic monitoring dashboard for ecological research",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
