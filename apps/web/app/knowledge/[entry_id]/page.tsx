import { KnowledgeDetail } from "@/components/knowledge-detail";

export default async function KnowledgeDetailPage({
  params,
}: {
  params: Promise<{ entry_id: string }>;
}) {
  const { entry_id: entryId } = await params;
  return <KnowledgeDetail entryId={entryId} />;
}
