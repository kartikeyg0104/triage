function slugify(text) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

const truncateSlug = (slug, max) => slug.slice(0, max);

module.exports = { slugify, truncateSlug };
