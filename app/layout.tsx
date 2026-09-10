import type { Metadata } from 'next';
import { Geist, Geist_Mono } from 'next/font/google';
import './globals.css';

const geistSans = Geist({
  variable: '--font-geist-sans',
  subsets: ['latin'],
});

const geistMono = Geist_Mono({
  variable: '--font-geist-mono',
  subsets: ['latin'],
});

export const metadata: Metadata = {
  metadataBase: new URL('https://ferc-intelligence.nathanjonez.chatgpt.site'),
  title: 'FERC Intelligence',
  description:
    'Source-backed operating asset and regulatory project intelligence.',
  alternates: {
    canonical: '/',
  },
  openGraph: {
    type: 'website',
    url: 'https://ferc-intelligence.nathanjonez.chatgpt.site',
    title: 'FERC Intelligence',
    description:
      'Source-backed operating asset and regulatory project intelligence.',
    images: [
      {
        url: 'https://ferc-intelligence.nathanjonez.chatgpt.site/og.png',
        width: 1200,
        height: 630,
        alt: 'FERC Intelligence',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'FERC Intelligence',
    description:
      'Source-backed operating asset and regulatory project intelligence.',
    images: ['https://ferc-intelligence.nathanjonez.chatgpt.site/og.png'],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
