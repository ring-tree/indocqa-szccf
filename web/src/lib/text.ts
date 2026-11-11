/** 把 Markdown 回答转换为纯文本，用于“复制纯文本”操作。 */
export function markdownToPlainText(source: string): string {
  let text = (source || "").replace(/\r\n?/g, "\n");
  text = text.replace(/```[^\n]*\n?([\s\S]*?)```/g, "$1");
  text = text.replace(/`([^`\n]+)`/g, "$1");
  text = text.replace(/\$\$([\s\S]*?)\$\$/g, "$1").replace(/\$([^$\n]+)\$/g, "$1");
  text = text.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  text = text.replace(/^\s{0,3}#{1,6}\s*/gm, "");
  text = text.replace(/^\s{0,3}>\s?/gm, "");
  text = text.replace(/\*\*\*(.+?)\*\*\*/g, "$1");
  text = text.replace(/\*\*(.+?)\*\*/g, "$1");
  text = text.replace(/__(.+?)__/g, "$1");
  text = text.replace(/~~(.+?)~~/g, "$1");
  text = text.replace(/(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])/g, "$1");
  text = text.replace(/(?<![\w_])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w_])/g, "$1");
  text = text.replace(/^\s*\|?[\s:|-]{3,}\|[\s:|-]*\|?\s*$/gm, "");
  text = text.replace(/^\s*\|(.+)\|[ \t]*$/gm, (_line, cells: string) =>
    cells
      .split("|")
      .map((cell) => cell.trim())
      .filter(Boolean)
      .join("，"),
  );
  text = text.replace(/^\s{0,3}[-*+]\s+\[[ xX]\]\s+/gm, "");
  text = text.replace(/^\s{0,3}[-*+]\s+/gm, "");
  text = text.replace(/^\s{0,3}(\d+)[.)]\s+/gm, "$1、");
  text = text.replace(/<\/?[A-Za-z][^>]*>/g, "");
  text = text.replace(/\\([\\`*_{}[\]()#+\-.!>|~])/g, "$1");
  return text
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/[ \t]{2,}/g, " ")
    .trim();
}
