import type { Message } from '../api/types';
import ReactMarkdown from 'react-markdown';

export function MessageBubble({ message }: { message: Message }) {
  const isUser = message.role === 'USER';

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[75%] px-4 py-3 rounded-xl text-sm ${
          isUser
            ? 'bg-primary-600 text-white rounded-br-sm'
            : message.role === 'TOOL'
            ? 'bg-amber-50 border border-amber-200 text-amber-900 rounded-bl-sm'
            : message.role === 'SYSTEM'
            ? 'bg-red-50 border border-red-200 text-red-700 rounded-bl-sm'
            : 'bg-white border border-surface-200 text-gray-800 rounded-bl-sm shadow-sm'
        }`}
      >
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <div className="prose prose-sm max-w-none">
            <ReactMarkdown>{message.content}</ReactMarkdown>
          </div>
        )}
        {message.tool_calls && (
          <details className="mt-1 text-xs opacity-70">
            <summary>Tool calls</summary>
            <pre className="mt-1 p-1 rounded bg-black/5 text-xs overflow-x-auto">
              {JSON.stringify(message.tool_calls, null, 2)}
            </pre>
          </details>
        )}
      </div>
    </div>
  );
}
