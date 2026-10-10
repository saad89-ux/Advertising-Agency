export const metadata = {
  title: "Launch Gate",
  description: "Pre-launch ad review",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
