// =============================================================================
// Freewrite CMS — block types
// =============================================================================
//
// Shape of the block-editor content that `<FreewriteCmsBlockRenderer />`
// renders. See the note at the bottom of this file for why the fetching half
// of the original module did not survive the move to a client-only SPA.
// =============================================================================

// Block Editor Types
// ---------------------------------------------------------------------------

export type BlockType = 'paragraph' | 'heading' | 'image' | 'code' | 'list' | 'quote' | 'divider';

export interface Block {
  id: string;
  type: BlockType;
  content: BlockContent;
  metadata?: Record<string, unknown>;
}

export type BlockContent =
  | ParagraphContent
  | HeadingContent
  | ImageContent
  | CodeContent
  | ListContent
  | QuoteContent
  | DividerContent;

export interface ParagraphContent {
  text: string;
  formatting?: TextFormatting[];
}

export interface HeadingContent {
  text: string;
  level: 1 | 2 | 3 | 4 | 5 | 6;
  formatting?: TextFormatting[];
}

export interface ImageContent {
  url: string;
  alt?: string;
  caption?: string;
  width?: number;
  height?: number;
}

export interface CodeContent {
  code: string;
  language?: string;
}

export interface ListContent {
  type: 'ordered' | 'unordered';
  items: string[];
}

export interface QuoteContent {
  text: string;
  author?: string;
}

export interface DividerContent {
  style?: 'solid' | 'dashed' | 'dotted';
}

export interface TextFormatting {
  type: 'bold' | 'italic' | 'underline' | 'strikethrough' | 'code' | 'link';
  start: number;
  end: number;
  url?: string;
}

// ---------------------------------------------------------------------------
// Fetching — deliberately not ported
// ---------------------------------------------------------------------------
//
// `fetchFreewriteCMS()` authenticated with `FREEWRITE_API_KEY` — a server-side
// secret — and relied on Next's `fetch(..., { next: { revalidate } })` ISR
// extension. Neither survives in a client-only SPA: the key would be visible
// in the bundle and the cache hint is a no-op.
//
// The types above are kept because `<FreeWriteCmsBlockRenderer />` is purely
// presentational and still renders `Block[]` from any source. If CMS content
// is wanted again, fetch it somewhere that can hold the key (a Supabase Edge
// Function, or the AWS API) and pass the blocks in as props.
