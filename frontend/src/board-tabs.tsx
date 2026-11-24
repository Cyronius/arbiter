import type {KeyboardEvent, ReactNode} from 'react';

export const tabs = [
  {id: 'sessions', label: 'Sessions'},
  {id: 'reservations', label: 'Reservations'},
  {id: 'approvals', label: 'Approvals'},
  {id: 'resources', label: 'Resources'},
  {id: 'messages', label: 'Messages'},
  {id: 'operator', label: 'Operator decisions'},
  {id: 'settings', label: 'Settings'},
] as const;

export type BoardTab = typeof tabs[number]['id'];

export function tabFromUrl(): BoardTab {
  const id = new URLSearchParams(location.search).get('tab');
  return tabs.find(tab => tab.id === id)?.id ?? tabs[0].id;
}

export function BoardTabs({selected, onSelect, children}: {
  selected: BoardTab; onSelect: (tab: BoardTab) => void; children: Record<BoardTab, ReactNode>;
}) {
  function navigate(event: KeyboardEvent<HTMLDivElement>) {
    const index = tabs.findIndex(tab => tab.id === selected);
    const destinations: Partial<Record<string, number>> = {
      ArrowRight: (index + 1) % tabs.length,
      ArrowLeft: (index + tabs.length - 1) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    };
    const nextIndex = destinations[event.key];
    if (nextIndex === undefined) return;
    event.preventDefault();
    const target = tabs[nextIndex].id;
    onSelect(target);
    document.getElementById(`tab-${target}`)?.focus();
  }

  return <>
    <div className="tab-bar" role="tablist" aria-label="Board areas" onKeyDown={navigate}>
      {tabs.map(tab => <button key={tab.id} id={`tab-${tab.id}`} type="button" role="tab" aria-controls={`area-${tab.id}`}
        aria-selected={tab.id === selected} tabIndex={tab.id === selected ? 0 : -1} onClick={() => onSelect(tab.id)}>{tab.label}</button>)}
    </div>
    <label className="tab-picker">Board area
      <select value={selected} onChange={event => {
        const tab = tabs.find(option => option.id === event.target.value);
        if (tab) onSelect(tab.id);
      }}>
        {tabs.map(tab => <option key={tab.id} value={tab.id}>{tab.label}</option>)}
      </select>
    </label>
    {tabs.map(tab => <div className="board-area" key={tab.id} id={`area-${tab.id}`} role="tabpanel" aria-label={tab.label}
      hidden={tab.id !== selected} tabIndex={0}>{children[tab.id]}</div>)}
  </>;
}
