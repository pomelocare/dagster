// Generated GraphQL types, do not edit manually.

import * as Types from '../../graphql/types';

export type GetScheduleQueryVariables = Types.Exact<{
  scheduleSelector: Types.ScheduleSelector;
  startTimestamp?: Types.InputMaybe<Types.Scalars['Float']>;
  ticksAfter?: Types.InputMaybe<Types.Scalars['Int']>;
  ticksBefore?: Types.InputMaybe<Types.Scalars['Int']>;
}>;

export type GetScheduleQuery = {
  __typename: 'DagitQuery';
  scheduleOrError:
    | {__typename: 'PythonError'; message: string; stack: Array<string>}
    | {__typename: 'Schedule'; name: string; potentialTickTimestamps: Array<number>}
    | {__typename: 'ScheduleNotFoundError'};
};

export type ScheduleDryRunMutationVariables = Types.Exact<{
  selectorData: Types.ScheduleSelector;
  timestamp?: Types.InputMaybe<Types.Scalars['Float']>;
}>;

export type ScheduleDryRunMutation = {
  __typename: 'DagitMutation';
  scheduleDryRun:
    | {
        __typename: 'DryRunInstigationTick';
        timestamp: number | null;
        evaluationResult: {
          __typename: 'TickEvaluation';
          skipReason: string | null;
          runRequests: Array<{
            __typename: 'RunRequest';
            runConfigYaml: string;
            runKey: string | null;
            tags: Array<{__typename: 'PipelineTag'; key: string; value: string}>;
          }> | null;
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
        } | null;
      }
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
    | {__typename: 'ScheduleNotFoundError'; scheduleName: string};
};
