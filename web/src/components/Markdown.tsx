import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];

/** 把 [n] 转成可点击的锚点链接，跳过代码块与公式，避免破坏数学矩阵。 */
function withCitationLinks(text: string): string {
  const protectedBlocks: string[] = [];
  const masked = text.replace(
    /(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$)/g,
    (block) => {
      protectedBlocks.push(block);
      return `@@B${protectedBlocks.length - 1}@@`;
    },
  );
  const linked = masked.replace(/\[(\d+)\]/g, (_m, n: string) => `[[${n}]](#cite-${n})`);
  return linked.replace(/@@B(\d+)@@/g, (_m, index: string) => protectedBlocks[Number(index)]);
}

interface Props {
  children: string;
  onCite?: (index: number) => void;
}

export default function Markdown({ children, onCite }: Props) {
  const components = useMemo<Components>(
    () => ({
      a({ href, children: linkChildren, ...props }) {
        if (href?.startsWith("#cite-")) {
          const index = Number(href.slice(6));
          return (
            <a
              {...props}
              href={href}
              onClick={(e) => {
                e.preventDefault();
                onCite?.(index);
              }}
              className="citation"
              aria-label={`查看引用 ${index}`}
              title={`查看引用 ${index}`}
            >
              {linkChildren}
            </a>
          );
        }
        return (
          <a {...props} href={href} target="_blank" rel="noreferrer">
            {linkChildren}
          </a>
        );
      },
    }),
    [onCite],
  );

  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={REMARK_PLUGINS}
        rehypePlugins={REHYPE_PLUGINS}
        components={components}
      >
        {withCitationLinks(children)}
      </ReactMarkdown>
    </div>
  );
}
