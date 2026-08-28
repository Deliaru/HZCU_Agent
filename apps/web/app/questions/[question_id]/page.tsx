import { QuestionsBoard } from "@/components/questions-board";

export default async function QuestionDetailPage({
  params,
}: {
  params: Promise<{ question_id: string }>;
}) {
  const { question_id: questionId } = await params;
  return <QuestionsBoard questionId={questionId} />;
}
