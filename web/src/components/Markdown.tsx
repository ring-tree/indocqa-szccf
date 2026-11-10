import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const REMARK_PLUGINS = [remarkGfm];

/** 把 [n] 转成可点击的锚点链接，交给上层滚动到来源卡片。 */
function withCitationLinks(text: string): string {
  return text.replace(/\[(\d+)\]/g, (_m, n: string) => `[[${n}]](#cite-${n})`);
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
      <ReactMarkdown remarkPlugins={REMARK_PLUGINS} components={components}>
        {withCitationLinks(children)}
      </ReactMarkdown>
    </div>
  );
}
