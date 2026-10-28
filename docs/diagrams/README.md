# Diagrams

The diagrams used in [WHITEPAPER.md](../../WHITEPAPER.md). Each has two files:

| File | Purpose |
|---|---|
| `NN-name.png` | The rendered image shown in the whitepaper (2x resolution) |
| `NN-name.mmd` | The editable [Mermaid](https://mermaid.js.org) source |

They are committed as images so they display the same everywhere. GitHub's own Mermaid renderer occasionally fails on pages with many diagrams ("Unable to render rich display").

## Changing a diagram

1. Edit the `.mmd` file. You can preview it at <https://mermaid.live> by pasting the contents.
2. Re-render the image with the Mermaid CLI (needs Node.js):

   ```bash
   npx -p @mermaid-js/mermaid-cli mmdc -i docs/diagrams/01-architecture.mmd \
        -o docs/diagrams/01-architecture.png -s 2 -b white
   ```

3. Commit both files.

The first line of each `.mmd` is an `%%{init: ...}%%` directive that keeps labels wrapped so the image stays a readable width.
