// Generated GraphQL types, do not edit manually.

import * as Types from '../../graphql/types';

export type TickTagFragment = {
  __typename: 'InstigationTick';
  id: string;
  status: Types.InstigationTickStatus;
  timestamp: number;
  skipReason: string | null;
  runIds: Array<string>;
  runKeys: Array<string>;
  error: {
    __typename: 'PythonError';
    message: string;
    stack: Array<string>;
    errorChain: Array<{
      __typename: 'ErrorChainLink';
      isExplicitLink: boolean;
      error: {__typename: 'PythonError'; message: string; stack: Array<string>};
    }>;
  } | null;
};

export type LaunchedRunListQueryVariables = Types.Exact<{
  filter: Types.RunsFilter;
}>;

export type LaunchedRunListQuery = {
  __typename: 'DagitQuery';
  pipelineRunsOrError:
    | {__typename: 'InvalidPipelineRunsFilterError'; message: string}
    | {
        __typename: 'PythonError';
        message: string;
        stack: Array<string>;
        errorChain: Array<{
          __typename: 'ErrorChainLink';
          isExplicitLink: boolean;
          error: {__typename: 'PythonError'; message: string; stack: Array<string>};
        }>;
      }
    | {
        __typename: 'Runs';
        results: Array<{
          __typename: 'Run';
          id: string;
          runId: string;
          status: Types.RunStatus;
          stepKeysToExecute: Array<string> | null;
          canTerminate: boolean;
          mode: string;
          rootRunId: string | null;
          parentRunId: string | null;
          pipelineSnapshotId: string | null;
          parentPipelineSnapshotId: string | null;
          pipelineName: string;
          solidSelection: Array<string> | null;
          startTime: number | null;
          endTime: number | null;
          updateTime: number | null;
          repositoryOrigin: {
            __typename: 'RepositoryOrigin';
            id: string;
            repositoryName: string;
            repositoryLocationName: string;
          } | null;
          assetSelection: Array<{__typename: 'AssetKey'; path: Array<string>}> | null;
          tags: Array<{__typename: 'PipelineTag'; key: string; value: string}>;
        }>;
      };
};
