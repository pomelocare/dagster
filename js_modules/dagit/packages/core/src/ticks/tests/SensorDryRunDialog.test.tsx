import {Resolvers} from '@apollo/client';
import {MockedProvider, MockedResponse} from '@apollo/client/testing';
import {act, render, screen, waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as React from 'react';

import {SensorDryRunDialog} from '../SensorDryRunDialog';

import {
  SensorDryRunMutationError,
  SensorDryRunMutationSkipped,
  SensorDryRunMutationRunRequests,
  PersistCursorValueMock,
} from './SensorDryRunDialog.mocks';

// This component is unit tested separately so mocking it out
jest.mock('../DryRunRequestTable', () => {
  return {
    RunRequestTable: () => <div />,
  };
});

const onCloseMock = jest.fn();

function Test({mocks, resolvers}: {mocks?: MockedResponse[]; resolvers?: Resolvers}) {
  return (
    <MockedProvider mocks={mocks} resolvers={resolvers}>
      <SensorDryRunDialog
        name="test"
        onClose={onCloseMock}
        isOpen={true}
        repoAddress={{
          name: 'testName',
          location: 'testLocation',
        }}
        jobName="testJobName"
        currentCursor="testCursor"
      />
    </MockedProvider>
  );
}

describe('SensorDryRunTest', () => {
  it('submits sensorDryRun mutation with cursor variable and renders successful result and persists cursor', async () => {
    await act(async () => {
      render(<Test mocks={[SensorDryRunMutationRunRequests, PersistCursorValueMock]} />);
    });
    const cursorInput = screen.getByTestId('cursor-input');
    userEvent.type(cursorInput, 'testing123');
    userEvent.click(screen.getByTestId('evaluate'));
    await waitFor(() => {
      expect(screen.getByText(/3\srun requests/g)).toBeVisible();
      expect(screen.queryByText('Skipped')).toBe(null);
      expect(screen.queryByText('Failed')).toBe(null);
    });
    userEvent.click(screen.getByTestId('persist-cursor'));
    expect(screen.getByText('Persisting')).toBeVisible();
    await waitFor(() => {
      expect(screen.getByText('Persisted')).toBeVisible();
    });
  });

  it('renders errors', async () => {
    await act(async () => {
      render(<Test mocks={[SensorDryRunMutationError]} />);
    });
    const cursorInput = screen.getByTestId('cursor-input');
    userEvent.type(cursorInput, 'testing123');
    userEvent.click(screen.getByTestId('evaluate'));
    await waitFor(() => {
      expect(screen.getByText('Failed')).toBeVisible();
      expect(screen.queryByText('Skipped')).toBe(null);
    });
  });

  it('renders skip reason', async () => {
    await act(async () => {
      render(<Test mocks={[SensorDryRunMutationSkipped]} />);
    });
    const cursorInput = screen.getByTestId('cursor-input');
    userEvent.type(cursorInput, 'testing123');
    userEvent.click(screen.getByTestId('evaluate'));
    await waitFor(() => {
      expect(screen.getByText('Skipped')).toBeVisible();
    });
  });
});
