import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { PLATFORMS, type Platform, type PlatformCopy, type PlatformCopyMap } from '@/lib/database.types';
import { titleCase } from '@/lib/format';

function parsePlatformCopy(value: unknown): PlatformCopyMap {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const record = value as Record<string, unknown>;
  const result: PlatformCopyMap = {};
  for (const platform of PLATFORMS) {
    const entry = record[platform];
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const { caption, title, description } = entry as Record<string, unknown>;
    result[platform] = {
      caption: typeof caption === 'string' ? caption : undefined,
      title: typeof title === 'string' ? title : undefined,
      description: typeof description === 'string' ? description : undefined,
    };
  }
  return result;
}

function CopyField({ label, value }: { label: string; value: string | undefined }) {
  if (!value) return null;
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="whitespace-pre-wrap text-sm leading-relaxed">{value}</p>
    </div>
  );
}

/**
 * One cut is four different pieces of writing. A TikTok caption, a YouTube
 * title and description, a LinkedIn post and an Instagram caption go out
 * together, so they are signed off together.
 */
export function PlatformCopyPanel({ value, targetPlatforms }: { value: unknown; targetPlatforms: string[] }) {
  const copy = parsePlatformCopy(value);
  const platforms = PLATFORMS.filter((platform) => copy[platform] !== undefined || targetPlatforms.includes(platform));

  if (platforms.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Per-platform copy</CardTitle>
          <CardDescription>No copy variants have been generated for this cut yet.</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const first = platforms[0] as Platform;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Per-platform copy</CardTitle>
        <CardDescription>What goes out alongside the video on each platform.</CardDescription>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue={first}>
          <TabsList>
            {platforms.map((platform) => (
              <TabsTrigger key={platform} value={platform}>
                {titleCase(platform)}
              </TabsTrigger>
            ))}
          </TabsList>
          {platforms.map((platform) => {
            const entry: PlatformCopy | undefined = copy[platform];
            return (
              <TabsContent key={platform} value={platform} className="space-y-4 pt-4">
                {entry ? (
                  <>
                    <CopyField label="Title" value={entry.title} />
                    <CopyField label="Caption" value={entry.caption} />
                    <CopyField label="Description" value={entry.description} />
                    {!entry.title && !entry.caption && !entry.description && (
                      <p className="text-sm text-muted-foreground">Nothing written for this platform yet.</p>
                    )}
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground">
                    This idea targets {titleCase(platform)}, but no copy has been generated for it.
                  </p>
                )}
              </TabsContent>
            );
          })}
        </Tabs>
      </CardContent>
    </Card>
  );
}
