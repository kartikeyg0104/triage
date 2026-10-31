// Turns titles into URL slugs.
export function slugify(text) {
  return text
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export class SlugRegistry {
  constructor() {
    this.taken = new Set();
  }

  reserve(title) {
    let slug = slugify(title);
    for (let n = 2; this.taken.has(slug); n++) {
      slug = `${slugify(title)}-${n}`;
    }
    this.taken.add(slug);
    return slug;
  }
}
