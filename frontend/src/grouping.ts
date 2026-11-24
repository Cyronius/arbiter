export const groupings = [
  {id: 'flat', label: 'Flat list'},
  {id: 'roster', label: 'Roster'},
  {id: 'flow', label: 'Flow'},
] as const;

export type Grouping = typeof groupings[number]['id'];

export function groupingFromUrl(): Grouping {
  const params = new URLSearchParams(location.search);
  const id = params.get('group') ?? params.get('concept');
  return groupings.find(group => group.id === id)?.id ?? 'flat';
}
