const fs = require('fs');
const path = require('path');

const inputPath = path.join(__dirname, 'newsUrl.json');
const outputPath = path.join(__dirname, 'newsUrl-links.json');

const html = fs.readFileSync(inputPath, 'utf8');

// Match h2.entry-title-post followed by <a href="URL" ...>
// Pattern: entry-title-post ... > ... <a href="URL"
const regex = /entry-title-post[^>]*>[\s\S]*?<a\s+href="([^"]+)"/g;
const links = [];
let match;
while ((match = regex.exec(html)) !== null) {
  links.push(match[1]);
}

// Remove duplicates while preserving order
const uniqueLinks = [...new Set(links)];

fs.writeFileSync(outputPath, JSON.stringify(uniqueLinks, null, 2), 'utf8');
console.log(`Extrahováno ${uniqueLinks.length} odkazů -> ${outputPath}`);
