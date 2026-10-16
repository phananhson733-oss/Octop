/** Preview is static document data. Opaque sandbox + CSP also block CSS network loads. */
export function staticReportHtml(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc
    .querySelectorAll(
      "script, iframe, frame, frameset, object, embed, form, base, meta, link",
    )
    .forEach((node) => node.remove());
  doc.querySelectorAll("*").forEach((node) => {
    for (const attr of Array.from(node.attributes)) {
      if (
        /^on/i.test(attr.name) ||
        attr.name.toLowerCase().endsWith(":href") ||
        [
          "href",
          "src",
          "srcset",
          "action",
          "formaction",
          "target",
          "ping",
          "background",
          "poster",
        ].includes(attr.name.toLowerCase())
      ) {
        node.removeAttribute(attr.name);
      }
    }
  });
  const policy = doc.createElement("meta");
  policy.httpEquiv = "Content-Security-Policy";
  policy.content =
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'";
  doc.head.prepend(policy);
  return `<!doctype html>${doc.documentElement.outerHTML}`;
}
